"""Record the real PyPI answers the PyPI tests replay. Run by hand, once,
with network access — never from the suite:

    uv run python tests/fixtures/pypi/record.py

Saves each project's PEP 691 page trimmed to its newest 30 versions and to
sdists, pure wheels and amd64 wheels — the targets the tests use
(`projects/<name>.json`, one file per line); the PEP 658 core metadata of
those wheels cut down to the headers the resolver reads
(`metadata/<sha256>.metadata`), with each file's `core-metadata` hash
rewritten to the cut-down body's so the fixtures stay self-consistent; and a
root index with the recorded serials (`root.json`). Prints which projects
carry a yanked file.
"""
import hashlib
import json
from pathlib import Path

import httpx
from packaging.utils import parse_sdist_filename, parse_wheel_filename

HERE = Path(__file__).parent
ACCEPT = {"Accept": "application/vnd.pypi.simple.v1+json"}
KEEP_VERSIONS = 30
PROJECTS = [
    "requests", "urllib3", "idna", "charset-normalizer", "certifi", "pysocks",
    "botocore", "jmespath", "python-dateutil", "six",
    "numpy", "cryptography", "cffi", "pycparser", "psycopg2", "pywin32",
]
FIELDS = ("filename", "url", "hashes", "size", "requires-python", "yanked")
HEADERS = ("Metadata-Version", "Name", "Version", "Requires-Python", "Requires-Dist", "Provides-Extra")


def wanted_file(filename: str) -> bool:
    if not filename.endswith(".whl"):
        return True
    platforms = parse_wheel_filename(filename)[3]
    return any(t.platform == "any" or t.platform.endswith("x86_64") or t.platform == "win_amd64"
               for t in platforms)


def cut(metadata: bytes) -> bytes:
    """Only the headers the resolver reads; the long description is most of the bytes."""
    head = metadata.split(b"\n\n", 1)[0].decode("utf-8", errors="replace")
    keep = [line for line in head.splitlines() if line.split(":", 1)[0] in HEADERS]
    return ("\n".join(keep) + "\n").encode()


def version_of(filename):
    try:
        if filename.endswith(".whl"):
            return parse_wheel_filename(filename)[1]
        if filename.endswith((".tar.gz", ".zip")):
            return parse_sdist_filename(filename)[1]
    except ValueError:
        return None
    return None


def main():
    (HERE / "projects").mkdir(exist_ok=True)
    (HERE / "metadata").mkdir(exist_ok=True)
    root = []
    with httpx.Client(timeout=60, follow_redirects=True,
                      headers={"User-Agent": "mmw fixture recorder"}) as client:
        for name in PROJECTS:
            page = client.get(f"https://pypi.org/simple/{name}/", headers=ACCEPT)
            page.raise_for_status()
            body = page.json()
            files = [f for f in body["files"]
                     if version_of(f["filename"]) is not None and wanted_file(f["filename"])]
            newest = sorted({version_of(f["filename"]) for f in files}, reverse=True)[:KEEP_VERSIONS]
            keep = [f for f in files if version_of(f["filename"]) in newest]
            serial = body["meta"]["_last-serial"]
            root.append({"name": name, "_last-serial": serial})
            cut_by_real: dict[str, str] = {}
            for f in keep:
                meta = f.get("core-metadata")
                if not (isinstance(meta, dict) and meta.get("sha256")):
                    continue
                real = meta["sha256"]
                if real not in cut_by_real:
                    r = client.get(f["url"] + ".metadata")
                    r.raise_for_status()
                    body_cut = cut(r.content)
                    cut_by_real[real] = hashlib.sha256(body_cut).hexdigest()
                    (HERE / "metadata" / f"{cut_by_real[real]}.metadata").write_bytes(body_cut)
            rows = []
            for f in keep:
                row = {k: f.get(k) for k in FIELDS}
                meta = f.get("core-metadata")
                row["core-metadata"] = ({"sha256": cut_by_real[meta["sha256"]]}
                                        if isinstance(meta, dict) and meta.get("sha256") else False)
                rows.append(json.dumps(row, sort_keys=True))
            wanted = cut_by_real
            (HERE / "projects" / f"{name}.json").write_text(
                '{"name": %s, "serial": %d, "files": [\n%s\n]}\n' % (json.dumps(name), serial, ",\n".join(rows)))
            yanked = [f["filename"] for f in keep if f.get("yanked")]
            print(f"{name}: {len(newest)} versions, {len(keep)} files, {len(wanted)} metadata"
                  + (f", yanked: {yanked[:3]}" if yanked else ""))
    (HERE / "root.json").write_text(json.dumps({"projects": root}, indent=1))


if __name__ == "__main__":
    main()
