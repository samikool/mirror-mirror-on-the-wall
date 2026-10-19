"""The online half of the PyPI pages: shard data, decided with `packaging`.

Pages are derived (spec §5.0): publish writes the network's published records
as 256 shards plus the stdlib renderer (`mmw/resources/pypi_render.py`),
which formats them wherever the tree is. Everything that needs `packaging` —
normalized names, version order, the version `pip install` would pick — is
decided here, so the renderer never needs it. File URLs stay the upstream
files.pythonhosted.org URLs, so lockfiles and hashes keep working. Output is
sorted, so the same records always give the same bytes.
"""
import gzip
import hashlib
import json

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from mmw.providers.pypi.records import Record
from mmw.resources import pypi_render

SHARD_DIR = "pypi-data/shards"
RENDERER = "pypi-data/render.py"


def _version_key(text: str):
    try:
        return (0, Version(text))
    except InvalidVersion:
        return (1, text)


def _latest(by_version: dict[str, list[Record]]) -> str:
    """The version `pip install <name>` would pick: newest non-yanked stable,
    else newest non-yanked, else newest."""
    ordered = sorted(by_version, key=_version_key, reverse=True)
    live = [v for v in ordered if not all(r.yanked for r in by_version[v])]
    stable = [v for v in live if _version_key(v)[0] == 0 and not _version_key(v)[1].is_prerelease]
    return (stable or live or ordered)[0]


def entry_of(name: str, records: list[Record], metadata: set[str]) -> dict:
    """One project's shard entry. `metadata`: filenames whose .metadata is
    published — only those are advertised."""
    files = sorted(records, key=lambda r: r.filename)
    by_version: dict[str, list[Record]] = {}
    for r in files:
        by_version.setdefault(r.version, []).append(r)
    return {
        "name": canonicalize_name(name),
        "versions": sorted(by_version, key=_version_key),
        "latest": _latest(by_version),
        "files": [{"filename": r.filename, "version": r.version, "url": r.url, "sha256": r.sha256,
                   "size": r.size, "requires_python": r.requires_python, "yanked": r.yanked,
                   "upload_time": r.upload_time,
                   "metadata": r.metadata_sha256 if r.filename in metadata and r.metadata_sha256 else None}
                  for r in files],
    }


def shard_key(name: str) -> str:
    return hashlib.sha256(canonicalize_name(name).encode()).hexdigest()[:2]


def shards(entries: list[dict]) -> dict[str, bytes]:
    """{"pypi-data/shards/<xx>.json.gz": bytes} — byte-stable (gzip mtime 0,
    sorted). Only shards with at least one project exist."""
    grouped: dict[str, dict] = {}
    for entry in entries:
        grouped.setdefault(shard_key(entry["name"]), {})[entry["name"]] = entry
    return {f"{SHARD_DIR}/{key}.json.gz":
            gzip.compress(json.dumps({"projects": grouped[key]}, sort_keys=True, separators=(",", ":")).encode(),
                          mtime=0)
            for key in sorted(grouped)}


def project_pages(name: str, records: list[Record], *, metadata: set[str]) -> dict[str, bytes]:
    return pypi_render.project_pages(entry_of(name, records, metadata))


def root_pages(names: list[str]) -> dict[str, bytes]:
    return pypi_render.root_pages({canonicalize_name(n) for n in names})
