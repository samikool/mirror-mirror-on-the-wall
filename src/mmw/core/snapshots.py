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
from mmw.core.pool import BundleInfo, Pool, SnapshotInfo, shared_pool_lock
from mmw.core.publisher.manifest import (
    bump_stamp, is_manifest, iter_manifest, ledger_dir, now_stamp,
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
                  progress=None, stop_event=None,
                  warnings: "list[str] | None" = None) -> SnapshotInfo:
    """Record `network`'s complete tree as a new snapshot. **Blocking.**

    Raises if the walk reported errors (a key that could not be frozen, a
    mirror missing from the config): a snapshot that silently lacks a file
    would not restore what it claims to.

    Holds the pool's shared lock throughout, as a sync does: keys and
    upstream files are frozen into the pool before the snapshot row that
    makes them GC roots is committed, so a prune in that window would delete
    them and leave a snapshot that can never be published or bundled to.
    """
    with shared_pool_lock(mirror_dir), Pool(mirror_dir) as pool:
        sink = RecordingSink(pool)
        result = NetworkPublishResult(network_name=network.name, dry_run=False)
        from mmw.core.publisher import walk_network
        mirrors = walk_network(network, cfg, mirror_dir, sink, pool, result,
                               nginx_root=None, prune=False,
                               progress=progress, stop_event=stop_event)
        if warnings is not None:
            warnings.extend(result.dist_upgrade_warnings)     # reported, never dropped
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("snapshot stopped — nothing was recorded")
        problems = sink.errors + result.errors
        if problems:
            raise ValueError(f"snapshot of {network.name} not taken: " + "; ".join(problems[:5])
                             + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""))
        from mmw.core.publisher.links import upstream_file_hosts, upstream_files
        from mmw.providers import extra_hosts
        settings = json.dumps({
            "network": network.model_dump(mode="json"),
            "mirrors": [m.legacy_dict() for m in mirrors],
            # nginx needs a server block per upstream-file host.
            "upstream_hosts": upstream_file_hosts(upstream_files(mirror_dir, mirrors)),
            # Hosts providers serve beyond their units' own (changelogs.ubuntu.com
            # for dist-upgrade), so a snapshot's nginx is regenerated exactly.
            "extra_hosts": extra_hosts(network, mirrors),
        }, sort_keys=True)
        stamp = _free_stamp(pool, network.name)
        sid = pool.create_snapshot(network.name, stamp, kind=kind, label=label,
                                   rows=sorted(sink.rows.items()), settings=settings)
        return next(s for s in pool.snapshots(network.name) if s.id == sid)


def recorded_bases(settings: str) -> list[str]:
    """The mirror_bases of a snapshot's recorded mirrors, longest first — so
    the first that prefixes a path is the one it belongs to. (`mirror_base`
    is a property: derived from each mirror, not stored.)"""
    from mmw.core.config import MirrorConfig
    mirrors = json.loads(settings or "{}").get("mirrors", [])
    return sorted({MirrorConfig.model_validate(m).mirror_base for m in mirrors},
                  key=len, reverse=True)


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


def served_by(pool: Pool, snap: SnapshotInfo) -> list[str]:
    """The directories currently serving `snap`."""
    return [p.root for p in pool.published(snap.network) if p.target == snap.stamp]


def last_bundled_to(pool: Pool, network: str) -> str | None:
    """Stamp the newest bundle of `network` went to — the next bundle's
    default starting point."""
    log = pool.bundles(network)
    return log[0].to_stamp if log else None


def delete_snapshot(pool: Pool, snap: SnapshotInfo) -> None:
    """Delete `snap`, unless a directory serves it: its rows are what says
    which files that tree should hold, and what publishing it again needs."""
    roots = served_by(pool, snap)
    if roots:
        raise ValueError(f"snapshot {snap.stamp} is published at {', '.join(roots)} — "
                         f"publish something else there first (e.g. `mmw publish -n "
                         f"{snap.network} --snapshot live`)")
    pool.delete_snapshot(snap.id)


@dataclass(frozen=True)
class SnapshotStatus:
    """One network's snapshot state, for a one-line summary."""
    snapshots: list[SnapshotInfo]          # newest first
    serving: str | None                    # what the network root serves: 'live' | stamp
    other_dirs: int                        # other directories it was published to
    last_bundle: BundleInfo | None

    def name(self, stamp: str | None) -> str:
        """A stamp with its label, if it has one."""
        if stamp is None:
            return "nothing (full)"
        label = next((s.label for s in self.snapshots if s.stamp == stamp and s.label), None)
        return f"{stamp} ({label})" if label else stamp


def snapshot_status(pool: Pool, network: str, root: Path | None) -> SnapshotStatus:
    published = pool.published(network)
    root_key = str(Path(root).resolve()) if root is not None else None
    serving = next((p.target for p in published if p.root == root_key), None)
    log = pool.bundles(network)
    return SnapshotStatus(pool.snapshots(network), serving,
                          sum(1 for p in published if p.root != root_key),
                          log[0] if log else None)


@dataclass(frozen=True)
class CleanPlan:
    delete: list[SnapshotInfo]     # oldest last
    frees: int                     # bytes the next prune frees once they are gone
    kept: list[tuple[SnapshotInfo, str]]   # past `keep` but kept anyway, and why


def clean_plan(pool: Pool, network: str, keep: int) -> CleanPlan:
    """What "keep the newest `keep` snapshots" would delete, and free. A
    snapshot a directory serves, or the last bundle went to, is never
    cleaned — delete it by name if it really must go."""
    last = last_bundled_to(pool, network)
    delete, kept = [], []
    for s in pool.snapshots(network)[max(keep, 0):]:
        roots = served_by(pool, s)
        if roots:
            kept.append((s, f"published at {', '.join(roots)}"))
        elif s.stamp == last:
            kept.append((s, "the last bundle went to it"))
        else:
            delete.append(s)
    return CleanPlan(delete, pool.snapshot_exclusive_bytes([s.id for s in delete]), kept)


def delete_snapshots(pool: Pool, snaps: list[SnapshotInfo]) -> None:
    for s in snaps:
        delete_snapshot(pool, s)


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
        if not is_manifest(path.name):
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
