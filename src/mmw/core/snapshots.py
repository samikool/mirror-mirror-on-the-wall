"""Network snapshots — a network's complete published tree at a moment, kept
in the pool database so it can later be bundled to (from any other snapshot,
forwards or backwards) or published again exactly.

A snapshot is taken by walking the network exactly as a publish does
(`publisher.walk_network`), into a `RecordingSink` that records rows instead
of writing files. So a snapshot cannot drift from what publish lays out.

- Package, index and by-hash files are pool blobs already: recorded by checksum.
- Keys and mirrored upstream files live outside the pool and a later sync
  rewrites them: they are copied into the pool first (frozen), then recorded.
- The nginx config embeds its destination root: never recorded, regenerated
  from the settings stored with the snapshot.

Every checksum a snapshot lists is a pool GC root (see `Pool.prune_orphans`).
"""
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from mmw.core.config import Config, NetworkConfig
from mmw.core.pool import Pool, SnapshotInfo
from mmw.core.publisher.manifest import (
    MANIFEST_SUFFIX, bump_stamp, iter_manifest, ledger_dir, now_stamp,
)
from mmw.core.publisher.results import NetworkPublishResult


class RecordingSink:
    """A `Sink` that records (path, checksum) instead of placing files."""

    def __init__(self, pool: Pool):
        self.pool = pool
        self.rows: dict[str, str] = {}
        self.errors: list[str] = []

    def link(self, src: Path, rel: str, size: int, checksum: str, kind: str) -> str:
        if not self.pool.contains(checksum):
            # A key or upstream file: freeze a COPY into the pool — `Pool.add`
            # moves the file it is given, and the original is the mirror's own.
            try:
                self.pool.add_copy(src, checksum, Path(rel).name, size)
            except (OSError, ValueError) as e:
                self.errors.append(f"{rel}: {e}")
                return "error"
        self.rows[rel] = checksum
        return "linked"

    def write(self, rel: str, data: bytes) -> None:
        if rel.startswith("nginx/"):
            return                  # embeds the destination root: regenerated
        checksum = hashlib.sha256(data).hexdigest()
        if not self.pool.contains(checksum):
            self.pool.add(data, checksum, "sha256", Path(rel).name, len(data))
        self.rows[rel] = checksum

    def finalize_group(self, base, keep, live_checksums, exclude) -> tuple[int, int]:
        return 0, 0

    def prune_stale_bases(self, live_bases) -> tuple[int, int]:
        return 0, 0

    def close(self) -> None:
        pass


def _free_stamp(pool: Pool, network: str) -> str:
    """A timestamp id no snapshot of this network has — two taken within one
    second (1 s resolution) must not collide."""
    taken = {s.stamp for s in pool.snapshots(network)}
    stamp = now_stamp()
    while stamp in taken:
        stamp = bump_stamp(stamp)
    return stamp


def take_snapshot(network: NetworkConfig, cfg: Config, mirror_dir: Path, *,
                  label: str | None = None, kind: str = "manual",
                  progress=None, stop_event=None) -> SnapshotInfo:
    """Record `network`'s complete tree as a new snapshot. **Blocking.**

    Raises if the walk reported errors (a key that could not be frozen, a
    mirror missing from the config): a snapshot that silently lacks a file
    would not restore what it claims to.
    """
    with Pool(mirror_dir) as pool:
        sink = RecordingSink(pool)
        result = NetworkPublishResult(network_name=network.name, dry_run=False)
        from mmw.core.publisher import walk_network
        mirrors = walk_network(network, cfg, mirror_dir, sink, pool, result,
                               nginx_root=None, prune=False,
                               progress=progress, stop_event=stop_event)
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("snapshot stopped — nothing was recorded")
        problems = sink.errors + result.errors
        if problems:
            raise ValueError(f"snapshot of {network.name} not taken: " + "; ".join(problems[:5])
                             + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""))
        settings = json.dumps({
            "network": network.model_dump(mode="json"),
            "mirrors": [m.model_dump(mode="json") for m in mirrors],
        }, sort_keys=True)
        stamp = _free_stamp(pool, network.name)
        sid = pool.create_snapshot(network.name, stamp, kind=kind, label=label,
                                   rows=sorted(sink.rows.items()), settings=settings)
        return next(s for s in pool.snapshots(network.name) if s.id == sid)


# ── finding, cleaning, importing ─────────────────────────────────────────────

def find_snapshot(pool: Pool, network: str, ref: str) -> SnapshotInfo:
    """A snapshot of `network` by stamp, label or numeric id. A label two
    snapshots share is refused rather than guessed."""
    snaps = pool.snapshots(network)
    for match in (
        [s for s in snaps if s.stamp == ref],
        [s for s in snaps if ref.isdigit() and s.id == int(ref)],
        [s for s in snaps if s.label == ref],
    ):
        if len(match) == 1:
            return match[0]
        if len(match) > 1:
            raise LookupError(f"{len(match)} snapshots of {network} are labelled {ref!r} — "
                              "use the stamp instead")
    raise LookupError(f"no snapshot {ref!r} for network {network!r}")


@dataclass(frozen=True)
class CleanPlan:
    delete: list[SnapshotInfo]     # oldest last
    frees: int                     # bytes the next prune frees once they are gone


def clean_plan(pool: Pool, network: str, keep: int) -> CleanPlan:
    """What "keep the newest `keep` snapshots" would delete, and free."""
    doomed = pool.snapshots(network)[max(keep, 0):]
    return CleanPlan(doomed, pool.snapshot_exclusive_bytes([s.id for s in doomed]))


def delete_snapshots(pool: Pool, snaps: list[SnapshotInfo]) -> None:
    for s in snaps:
        pool.delete_snapshot(s.id)


def import_ledger(pool: Pool, mirror_dir: Path, network: str) -> int:
    """Bring the bundle manifests written before snapshots existed in as
    snapshots, so a far side sitting at one of them can still get a delta.
    Their lists are exact, but files pruned since may be gone: `complete =
    False` — a starting point, never a destination. Idempotent; returns how
    many were imported."""
    d = ledger_dir(mirror_dir, network)
    if not d.is_dir():
        return 0
    have = {s.stamp for s in pool.snapshots(network)}
    imported = 0
    for path in sorted(d.iterdir()):
        if not path.name.endswith(MANIFEST_SUFFIX):
            continue
        header, rows = iter_manifest(path)
        if header.created in have:
            rows.close()
            continue
        pool.create_snapshot(
            network, header.created, kind="imported", complete=False,
            rows=((p, c) for p, c in rows if not p.startswith("nginx/")))
        imported += 1
    return imported
