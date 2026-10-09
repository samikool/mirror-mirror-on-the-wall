"""Bundle manifests: what a network's tree is expected to contain.

A manifest describes the **complete** tree at a point in time, not the delta
that produced it. That is what makes `apply.py --verify` meaningful and what
makes a diverged site recoverable later.

JSONL — a header line then one line per file — so the far side can stream it
rather than hold millions of entries in memory. zstd-compressed on disk.

Timestamps, not generation numbers: `20260906T143000Z`. Newest wins.
"""
import calendar
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


def bump_stamp(stamp: str) -> str:
    """The next second after `stamp`, same format.

    `now_stamp()` has 1-second resolution, so two bundles started within the
    same wall-clock second would otherwise collide on both the ledger
    manifest filename and the output tarball filename — the second run's
    `.replace()` silently destroying the first run's record of what the far
    side was actually sent (and, when both stamps are equal, making
    `previous == created`, which degenerates the out-of-order check).
    `bundle_network` bumps forward until neither exists on disk.
    """
    t = calendar.timegm(time.strptime(stamp, TS_FORMAT))
    return time.strftime(TS_FORMAT, time.gmtime(t + 1))


def ledger_dir(mirror_dir: Path, network: str) -> Path:
    return mirror_dir / "bundles" / network


@dataclass(frozen=True)
class ManifestHeader:
    network: str
    created: str
    previous: str | None
    full: bool
    # How many real files (payload + generated, never the fixed .mmw/
    # control members) THIS bundle's tar actually contains — set by
    # BundleSink.close() once its own send/generate lists are final, just
    # before the manifest is written. Never confused with the manifest's own
    # (always-complete) file list: a delta bundle's file_count is correctly
    # much smaller than len(files), since most of the tree is carried
    # forward unchanged. Optional and defaulted so an older manifest already
    # on disk — written before this field existed — still parses; apply.py
    # treats its absence as "can't check this, don't try."
    file_count: int | None = None


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

    Memory note: measured at roughly 1.5 GB peak for a 2M-file network, not
    the ~600 MB once documented here — that figure only counted this dict;
    `manifest_rows` + `BundleSink._send`/`_seen` cost another ~440 MB on top
    of it during a bundle, and `--full` no longer skips this load at all (it
    still needs `prev` to compute the delete set — see bundle.py). A
    streaming refactor would fix this properly; deferred
    "Next up". If it bites before then, the format is internal — swap JSONL
    for a SQLite manifest without changing any caller.
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


def union_manifests(mirror_dir: Path, network: str) -> dict[str, str]:
    """Every path any retained ledger manifest ever claimed, newest winning.

    `--full` means "send everything and reconcile what the far side has".
    The delete set is `prev − keep`, and `prev` used to be the single latest
    manifest — so a path dropped three bundles ago appears in NO delete list
    ever again: manifest N-2 named it, N-1 and N do not, and only N is read.
    A far side that missed those bundles (the exact situation `--full` is
    reached for) keeps serving it forever.

    Unioning every retained manifest is the best a producer can do without
    far-side state: it recovers any divergence that happened within the
    ledger's retention. A path removed before the OLDEST retained manifest
    was written is still unrecoverable this way — carrying far-side state
    back (`--since state.json`) is the only real fix and is not built.

    Newest-wins on the checksum so the map still describes the far side's
    most recently known content. Memory is the same ceiling `read_manifest`
    documents, multiplied by the number of retained manifests — deliberate,
    and only paid on `--full`.
    """
    d = ledger_dir(mirror_dir, network)
    if not d.is_dir():
        return {}
    merged: dict[str, str] = {}
    for path in sorted(p for p in d.iterdir() if p.name.endswith(MANIFEST_SUFFIX)):
        try:
            _hdr, files = read_manifest(path)
        except Exception:
            # Broad on purpose: a corrupt zstd frame, a truncated line, a
            # header from a future version. A manifest we can no longer read
            # is not a reason to abandon the delete set the readable ones
            # give us — and this runs only to compute deletions, never to
            # decide what to send.
            continue
        merged.update(files)
    return merged


def delta(prev: dict[str, str], cur: dict[str, str]) -> tuple[set[str], set[str]]:
    """(send, delete) — a set difference over rows the pool already holds.

    No payload I/O and no hashing: this is why bundles stream from the pool
    rather than from a published tree, where a delta would mean re-hashing
    every file.
    """
    send = {rel for rel, cs in cur.items() if prev.get(rel) != cs}
    dele = {rel for rel in prev if rel not in cur}
    return send, dele
