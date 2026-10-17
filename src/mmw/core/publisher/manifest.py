"""Bundle manifests: what a network's tree is expected to contain.

A manifest describes the **complete** tree at a point in time, not the delta
that produced it. That is what makes `apply.py --verify` meaningful and what
makes a diverged site recoverable later.

JSONL — a header line then one line per file — so the far side can stream it
rather than hold millions of entries in memory. Plain text: apply.py reads it
with nothing but Python. Ledger files written before that are zstd
(`.manifest.jsonl.zst`) and are still read here.

Timestamps, not generation numbers: `20260906T143000Z`. Newest wins.
"""
import calendar
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

TS_FORMAT = "%Y%m%dT%H%M%SZ"
MANIFEST_SUFFIX = ".manifest.jsonl"
_LEGACY_SUFFIX = ".manifest.jsonl.zst"      # ledger files from before plain manifests
_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def is_manifest(name: str) -> bool:
    return name.endswith((MANIFEST_SUFFIX, _LEGACY_SUFFIX))


def _open_text(path: Path):
    """The manifest's text, streamed — through zstd for a legacy ledger file."""
    import io
    raw = open(path, "rb")
    if raw.read(4) != _ZSTD_MAGIC:
        raw.seek(0)
        return io.TextIOWrapper(raw, encoding="utf-8")
    raw.seek(0)
    import zstandard
    return io.TextIOWrapper(zstandard.ZstdDecompressor().stream_reader(raw, closefd=True),
                            encoding="utf-8")


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
    # The snapshots' labels, for people: printed by apply.py and kept in its
    # state file. Optional — older manifests and apply.py ignore them.
    previous_label: str | None = None
    created_label: str | None = None


def write_manifest(path: Path, header: ManifestHeader,
                   files: Iterable[tuple[str, str]]) -> None:
    """Write header + one {"p":…,"c":…} line per file.

    Streams: `files` may be a generator over millions of rows.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as out:
            out.write(json.dumps(asdict(header), sort_keys=True) + "\n")
            for rel, checksum in files:
                out.write(json.dumps({"p": rel, "c": checksum}) + "\n")
        tmp.replace(path)
    finally:
        # A no-op after a successful replace() — tmp no longer exists there,
        # so this only cleans up the partial file left by a `files` generator
        # that raised mid-stream (a real path: a DB error while walking pool
        # rows).
        tmp.unlink(missing_ok=True)


def read_manifest(path: Path) -> tuple[ManifestHeader, dict[str, str]]:
    """Load a manifest fully. (`iter_manifest` streams it.)"""
    header, rows = iter_manifest(path)
    return header, dict(rows)


def iter_manifest(path: Path):
    """(header, rows) with `rows` a generator of (path, checksum) — streamed,
    so a 700k-file manifest never has to be held whole."""
    text = _open_text(path)
    header = ManifestHeader(**json.loads(text.readline()))

    def rows():
        try:
            for line in text:
                if line.strip():
                    row = json.loads(line)
                    yield row["p"], row["c"]
        finally:
            text.close()
    return header, rows()


def latest_manifest_path(mirror_dir: Path, network: str) -> Path | None:
    """Newest manifest by timestamp, or None if this network was never bundled."""
    d = ledger_dir(mirror_dir, network)
    if not d.is_dir():
        return None
    stamps = sorted(p for p in d.iterdir() if is_manifest(p.name))
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


# What a destination was last given: a publish writes it into its tree, and
# apply.py into the tree a bundle lands in — the bundle manifest's own name.
RECORD_PATH = ".mmw/manifest.jsonl"


def unchanged(prev: dict[str, str] | None, rel: str, checksum: str, full: bool) -> bool:
    """Whether a destination already holds `rel` at `checksum` — the one rule
    both sinks decide "skip" by. An empty checksum is never trusted."""
    return not full and bool(checksum) and prev is not None and prev.get(rel) == checksum


def read_record(root: Path, network: str) -> dict[str, str] | None:
    """The tree's record, or None: absent, unreadable, or another network's
    (a reused root must not be trusted on a stranger's word)."""
    path = Path(root) / RECORD_PATH
    try:
        header, rows = iter_manifest(path)
        if header.network != network:
            rows.close()
            return None
        return dict(rows)
    except (OSError, ValueError, TypeError, KeyError):
        return None
