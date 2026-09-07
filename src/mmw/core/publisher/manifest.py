"""Bundle manifests: what a network's tree is expected to contain.

A manifest describes the **complete** tree at a point in time, not the delta
that produced it. That is what makes `apply.py --verify` meaningful and what
makes a diverged site recoverable later.

JSONL — a header line then one line per file — so the far side can stream it
rather than hold millions of entries in memory. zstd-compressed on disk.

Timestamps, not generation numbers: `20260906T143000Z`. Newest wins.
"""
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import zstandard

TS_FORMAT = "%Y%m%dT%H%M%SZ"
MANIFEST_SUFFIX = ".manifest.jsonl.zst"


def now_stamp() -> str:
    return time.strftime(TS_FORMAT, time.gmtime())


def ledger_dir(mirror_dir: Path, network: str) -> Path:
    return mirror_dir / "bundles" / network


@dataclass(frozen=True)
class ManifestHeader:
    network: str
    created: str
    previous: str | None
    full: bool


def write_manifest(path: Path, header: ManifestHeader,
                   files: Iterable[tuple[str, str]]) -> None:
    """Write header + one {"p":…,"c":…} line per file, zstd-compressed.

    Streams: `files` may be a generator over millions of rows.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    cctx = zstandard.ZstdCompressor(level=10)
    try:
        with open(tmp, "wb") as raw, cctx.stream_writer(raw) as zw:
            zw.write((json.dumps(asdict(header), sort_keys=True) + "\n").encode())
            for rel, checksum in files:
                zw.write((json.dumps({"p": rel, "c": checksum}) + "\n").encode())
        tmp.replace(path)
    finally:
        # A no-op after a successful replace() — tmp no longer exists there,
        # so this only cleans up the partial file left by a `files` generator
        # that raised mid-stream (a real path: a DB error while walking pool
        # rows).
        tmp.unlink(missing_ok=True)


def read_manifest(path: Path) -> tuple[ManifestHeader, dict[str, str]]:
    """Load a manifest fully.

    Memory note: a 2M-file network costs roughly 600 MB as a Python dict. That
    is the documented bound for delta computation; `--full` skips this load
    entirely. If it bites, the format is internal — swap JSONL for a SQLite
    manifest without changing any caller.
    """
    dctx = zstandard.ZstdDecompressor()
    files: dict[str, str] = {}
    with open(path, "rb") as raw, dctx.stream_reader(raw) as zr:
        text = zr.read().decode()
    lines = text.splitlines()
    header = ManifestHeader(**json.loads(lines[0]))
    for line in lines[1:]:
        if not line:
            continue
        row = json.loads(line)
        files[row["p"]] = row["c"]
    return header, files


def latest_manifest_path(mirror_dir: Path, network: str) -> Path | None:
    """Newest manifest by timestamp, or None if this network was never bundled."""
    d = ledger_dir(mirror_dir, network)
    if not d.is_dir():
        return None
    stamps = sorted(p for p in d.iterdir() if p.name.endswith(MANIFEST_SUFFIX))
    return stamps[-1] if stamps else None


def delta(prev: dict[str, str], cur: dict[str, str]) -> tuple[set[str], set[str]]:
    """(send, delete) — a set difference over rows the pool already holds.

    No payload I/O and no hashing: this is why bundles stream from the pool
    rather than from a published tree, where a delta would mean re-hashing
    every file.
    """
    send = {rel for rel, cs in cur.items() if prev.get(rel) != cs}
    dele = {rel for rel in prev if rel not in cur}
    return send, dele
