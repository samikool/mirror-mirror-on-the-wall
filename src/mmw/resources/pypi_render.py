"""Render a tree's PyPI pages from its shards — stdlib only.

Shipped as `pypi-data/render.py` beside the shards it reads, and run by one
rule (`render_derived` in apply.py): after a live publish, after a snapshot
replay, and on the far side after a bundle is applied. The pages under
`pypi.org/` are derived, never shipped: snapshots and bundles carry 256
shards instead of millions of pages.

Everything that needs `packaging` (normalized names, version order, the
version `pip install` would pick) was decided online and is in the shard;
this only formats it. A shard whose checksum matches the last render is
skipped; a changed one re-renders its projects and removes vanished ones.
"""
import gzip
import hashlib
import json
import os
import shutil
from html import escape

API_VERSION = "1.1"
OUT = "pypi.org"
STATE = ".mmw-rendered"
SHARDS = os.path.join("pypi-data", "shards")


def _json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def _anchor(f):
    attrs = ['href="%s#sha256=%s"' % (escape(f["url"]), f["sha256"])]
    if f["requires_python"]:
        attrs.append('data-requires-python="%s"' % escape(f["requires_python"]))
    if f["yanked"]:
        attrs.append('data-yanked="%s"' % escape(f["yanked"] if isinstance(f["yanked"], str) else ""))
    if f["metadata"]:
        attrs += ['data-core-metadata="sha256=%s"' % f["metadata"],
                  'data-dist-info-metadata="sha256=%s"' % f["metadata"]]
    return "<a %s>%s</a><br />" % (" ".join(attrs), escape(f["filename"]))


def _html(title, body):
    return ("<!DOCTYPE html>\n<html>\n<head>\n"
            '<meta name="pypi:repository-version" content="%s">\n' % API_VERSION
            + "<title>%s</title>\n</head>\n<body>\n" % escape(title)
            + "\n".join(body) + "\n</body>\n</html>\n").encode()


def _simple_file(f):
    meta = {"sha256": f["metadata"]} if f["metadata"] else False
    entry = {"filename": f["filename"], "url": f["url"], "hashes": {"sha256": f["sha256"]}, "size": f["size"],
             "requires-python": f["requires_python"], "yanked": f["yanked"],
             "core-metadata": meta, "data-dist-info-metadata": meta}
    if f["upload_time"]:
        entry["upload-time"] = f["upload_time"]
    return entry


def _api_file(f):
    return {"filename": f["filename"], "url": f["url"], "digests": {"sha256": f["sha256"]}, "size": f["size"],
            "requires_python": f["requires_python"], "yanked": bool(f["yanked"]),
            "yanked_reason": f["yanked"] if isinstance(f["yanked"], str) else None,
            "packagetype": "bdist_wheel" if f["filename"].endswith(".whl") else "sdist",
            "upload_time_iso_8601": f["upload_time"]}


def project_pages(entry):
    """{path under pypi.org/: bytes} for one shard entry."""
    name, files = entry["name"], entry["files"]
    by_version = {}
    for f in files:
        by_version.setdefault(f["version"], []).append(f)
    return {
        "simple/%s/index.html" % name: _html("Links for %s" % name,
                                             ["<h1>Links for %s</h1>" % escape(name)]
                                             + [_anchor(f) for f in files]),
        "simple/%s/index.json" % name: _json({
            "meta": {"api-version": API_VERSION}, "name": name, "versions": entry["versions"],
            "files": [_simple_file(f) for f in files]}),
        "pypi/%s/json" % name: _json({
            "info": {"name": name, "version": entry["latest"]},
            "releases": {v: [_api_file(f) for f in by_version[v]] for v in entry["versions"]},
            "urls": [_api_file(f) for f in by_version[entry["latest"]]]}),
    }


def root_pages(names):
    names = sorted(names)
    return {
        "simple/index.html": _html("Simple index",
                                   ['<a href="/simple/%s/">%s</a><br />' % (n, escape(n)) for n in names]),
        "simple/index.json": _json({"meta": {"api-version": API_VERSION}, "projects": [{"name": n} for n in names]}),
    }


def _write(path, data):
    """Staged and renamed, and only when the bytes differ: a client mid-read
    sees the old page or the new one, and an unchanged page keeps its mtime."""
    try:
        with open(path, "rb") as fh:
            if fh.read() == data:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, path)
    return True


def _remove_project(out, name):
    for rel in ("simple", "pypi"):
        shutil.rmtree(os.path.join(out, rel, name), ignore_errors=True)


def render(root, say=None):
    """Bring `root/pypi.org/` in line with `root/pypi-data/shards/`. Returns
    (pages written, projects removed)."""
    root = str(root)
    out = os.path.join(root, OUT)
    state_dir = os.path.join(out, STATE)
    state_path = os.path.join(state_dir, "shards.json")
    try:
        with open(state_path) as fh:
            previous = json.load(fh)
    except (OSError, ValueError):
        previous = {}
    shard_dir = os.path.join(root, SHARDS)
    names = sorted(n for n in os.listdir(shard_dir) if n.endswith(".json.gz")) if os.path.isdir(shard_dir) else []
    current, written, removed, changed = {}, 0, 0, False
    for i, shard in enumerate(names, start=1):
        with open(os.path.join(shard_dir, shard), "rb") as fh:
            blob = fh.read()
        digest = hashlib.sha256(blob).hexdigest()
        known = previous.get(shard)
        if known and known[0] == digest and os.path.isdir(os.path.join(out, "simple")):
            current[shard] = known
        else:
            changed = True
            projects = json.loads(gzip.decompress(blob))["projects"]
            for entry in projects.values():
                for rel, data in project_pages(entry).items():
                    written += _write(os.path.join(out, rel), data)
            for gone in set(known[1] if known else []) - set(projects):
                _remove_project(out, gone)
                removed += 1
            current[shard] = [digest, sorted(projects)]
        if say and (i % 16 == 0 or i == len(names)):
            say("PyPI pages: %d/%d shards rendered" % (i, len(names)))
    for shard in set(previous) - set(current):
        for gone in previous[shard][1]:
            _remove_project(out, gone)
            removed += 1
        changed = True
    if changed or not os.path.exists(os.path.join(out, "simple", "index.html")):
        everyone = [n for _digest, projects in current.values() for n in projects]
        for rel, data in root_pages(everyone).items():
            written += _write(os.path.join(out, rel), data)
    os.makedirs(state_dir, exist_ok=True)
    _write(state_path, json.dumps(current, sort_keys=True).encode())
    if say:
        say("PyPI pages: %d written, %d projects removed" % (written, removed))
    return written, removed


if __name__ == "__main__":
    import sys
    render(sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
           say=print)
