"""Network snapshots — a network's complete published tree at a moment, kept
in the pool database so it can later be bundled to (from any other snapshot,
forwards or backwards) or published again exactly.

A snapshot is taken by walking the network exactly as a publish does
(`publisher.walk_network`), into a `RecordingSink` that records rows instead
of writing files. So a snapshot cannot drift from what publish lays out.

- Package, index and by-hash files are pool blobs already: recorded by checksum.
- Keys and mirrored upstream files live outside the pool and a later sync
  rewrites them: they are copied into the pool first (frozen), then recorded.
- The nginx image is frozen like any other file; generated container setup
  is regenerated from the settings stored with the snapshot.

Every checksum a snapshot lists is a pool GC root (see `Pool.prune_orphans`).
"""
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from mmw.core import disk_io
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

    def link_pooled(self, src: Path, rel: str, size: int, checksum: str, kind: str) -> str:
        """`link` for a `mirror_files` row: pooled by construction, so no
        `contains` — 515k rows cost 4.4 s of per-row lookups (measured)."""
        self.rows[rel] = checksum
        return "linked"

    def write(self, rel: str, data: bytes) -> None:
        if rel.startswith("nginx/"):
            return                  # generated setup; image arrives through link()
        checksum = hashlib.sha256(data).hexdigest()
        if not self.pool.contains(checksum):
            self.pool.add(data, checksum, "sha256", Path(rel).name, len(data))
        self.rows[rel] = checksum

    def finalize_group(self, base, keep, live_checksums, exclude) -> tuple[int, int]:
        return 0, 0

    def drop_if_empty(self, base: str) -> None:
        pass                            # snapshots record files, not directories

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
            warnings.extend(result.warnings)
        if stop_event is not None and stop_event.is_set():
            raise InterruptedError("snapshot stopped — nothing was recorded")
        problems = sink.errors + result.errors
        if problems:
            raise ValueError(f"snapshot of {network.name} not taken: " + "; ".join(problems[:5])
                             + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""))
        from mmw.core.publisher.links import upstream_file_hosts, upstream_files
        from mmw.providers import extra_hosts
        settings = json.dumps({
            "nginx_container": 2,
            "network": network.model_dump(mode="json"),
            "mirrors": [m.legacy_dict() for m in mirrors],
            # nginx needs a server block per upstream-file host.
            "upstream_hosts": upstream_file_hosts(upstream_files(mirror_dir, mirrors)),
            # The bases the walk published (providers choose them); replay and
            # bundles read these rather than re-deriving them from mirror_base.
            "bases": result.bases,
            # Hosts providers serve beyond their units' own (changelogs.ubuntu.com
            # for dist-upgrade), so a snapshot's nginx is regenerated exactly.
            "extra_hosts": extra_hosts(network, mirrors),
            # Which repository each base belongs to: what versions and the
            # far side track (`recorded_repos`).
            "repos": _group_repos(result.base_repos),
        }, sort_keys=True)
        # Older snapshots first, so version numbers follow time even when a
        # snapshot is the first thing taken after upgrading.
        ensure_repo_versions(pool, network.name)
        stamp = _free_stamp(pool, network.name)
        rows = sorted(sink.rows.items())
        sid = pool.create_snapshot(network.name, stamp, kind=kind, label=label,
                                   rows=rows, settings=settings)
        pool.record_repo_versions(sid, network.name, stamp,
                                  repo_digests(rows, recorded_repos(settings)))
        return next(s for s in pool.snapshots(network.name) if s.id == sid)


def recorded_bases(settings: str) -> list[str]:
    """The bases a snapshot's tree was published under, longest first — so the
    first that prefixes a path is the one it belongs to. Recorded since
    providers choose their own bases; derived from each recorded mirror's
    `mirror_base` for snapshots taken before that."""
    from mmw.core.config import MirrorConfig
    data = json.loads(settings or "{}")
    bases = data.get("bases")
    if bases is None:
        bases = {MirrorConfig.model_validate(m).mirror_base for m in data.get("mirrors", [])}
    return sorted(set(bases), key=len, reverse=True)


def _group_repos(base_repos: dict[str, str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for base, repo in base_repos.items():
        grouped.setdefault(repo, []).append(base)
    return {repo: sorted(bases, key=len, reverse=True) for repo, bases in grouped.items()}


def recorded_repos(settings: str) -> dict[str, list[str]]:
    """Repository key → its bases (longest first) for a snapshot. Recorded
    since repositories were versioned; derived for snapshots taken before:
    an apt mirror's base is its source's, a base a "provider"-scope
    provider claims is that provider's, and what is left goes to the one
    such provider recorded that claimed nothing, if there is exactly one."""
    from mmw.core.config import MirrorConfig
    from mmw.providers import get_provider
    data = json.loads(settings or "{}")
    if "repos" in data:
        return {r: list(b) for r, b in data["repos"].items()}
    mirrors = [MirrorConfig.model_validate(m) for m in data.get("mirrors", [])]
    scoped = []
    base_repos: dict[str, str] = {}
    for m in mirrors:
        provider = get_provider(m.provider)
        if provider.repository_scope == "provider":
            if provider not in scoped:
                scoped.append(provider)
        else:
            base_repos[m.mirror_base] = m.source
    claimed_by: set[str] = set()
    leftover = []
    for base in recorded_bases(settings):
        if base in base_repos:
            continue
        owner = next((p for p in scoped if p.claims_base(base)), None)
        if owner is not None:
            base_repos[base] = owner.name
            claimed_by.add(owner.name)
        else:
            leftover.append(base)
    rest = [p for p in scoped if p.name not in claimed_by]
    if len(rest) == 1:
        base_repos.update(dict.fromkeys(leftover, rest[0].name))
    return _group_repos(base_repos)


def repo_digests(rows, repo_bases: dict[str, list[str]]) -> dict[str, str]:
    """A digest per repository of its (path, checksum) rows — equal digests,
    equal files. Every repository gets one, even with no files; rows under
    no repository's base (network-level files) count for none."""
    from mmw.core.repos import attribute
    hashes = {repo: hashlib.sha256() for repo in repo_bases}
    for path, checksum in sorted(rows):
        repo = attribute(path, repo_bases)
        if repo is not None:
            hashes[repo].update(f"{path}\0{checksum}\n".encode())
    return {repo: h.hexdigest() for repo, h in hashes.items()}


def ensure_repo_versions(pool: Pool, network: str) -> int:
    """Give every complete snapshot of `network` that has no repository
    versions its versions, oldest first so the numbers follow time.
    History-only (imported) snapshots get none. Returns how many."""
    done = 0
    for snap in reversed(pool.snapshots(network)):
        if not snap.complete or pool.snapshot_repo_versions(snap.id):
            continue
        pool.record_repo_versions(snap.id, network, snap.stamp,
                                  repo_digests(pool.snapshot_files(snap.id), recorded_repos(snap.settings)))
        done += 1
    return done


def repo_version_files(pool: Pool, version):
    """A repository version's (path, checksum) rows, from the newest
    snapshot holding it; None once no snapshot does (its content is gone)."""
    from mmw.core.repos import attribute
    holders = pool.repo_version_snapshots(version.id)
    if not holders:
        return None
    snap = next(s for s in pool.snapshots(version.network) if s.id == holders[0])
    repo_bases = recorded_repos(snap.settings)
    return ((path, checksum) for path, checksum in pool.snapshot_files(snap.id)
            if attribute(path, repo_bases) == version.repo)


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


def delete_snapshot(pool: Pool, snap: SnapshotInfo) -> bool:
    """Delete `snap` — free the files only it holds. One a transfer went to
    is kept as history only: a far side may sit at it, so the next transfer
    still starts from its list, and the ledger manifest under its stamp would
    only import it again. Refused while a directory serves it (its rows say
    which files that tree should hold) and for a history-only one (it holds
    nothing). Returns whether it was kept as history only."""
    roots = served_by(pool, snap)
    if roots:
        raise ValueError(f"snapshot {snap.stamp} is published at {', '.join(roots)} — "
                         f"publish something else there first (e.g. `mmw publish -n "
                         f"{snap.network} --snapshot live`)")
    if not snap.complete:
        raise ValueError(f"snapshot {snap.stamp} is history only — it holds no files, "
                         "so deleting it frees nothing")
    if any(b.to_stamp == snap.stamp for b in pool.bundles(snap.network)):
        pool.demote_snapshot(snap.id)
        return True
    pool.delete_snapshot(snap.id)
    return False


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


def clean_plan(pool: Pool, network: str, keep: int, cfg: Config | None = None) -> CleanPlan:
    """What "keep the newest `keep` snapshots" would delete, and free. A
    snapshot a directory serves, or the last bundle went to, is never
    cleaned — delete it by name if it really must go."""
    last = last_bundled_to(pool, network)
    sent = set(pool.last_sent_versions(network).values())
    delete, kept = [], []
    # History-only snapshots hold nothing to free, and don't count as kept.
    candidates = [s for s in pool.snapshots(network) if s.complete][max(keep, 0):]
    doomed = {s.id for s in candidates}
    for s in candidates:
        roots = served_by(pool, s)
        # A version the ledger says the far side has, which no snapshot
        # outside this clean holds: the next delta of that repository
        # starts from it.
        only = [v for v in pool.snapshot_repo_versions(s.id).values()
                if v.id in sent and set(pool.repo_version_snapshots(v.id)) <= doomed]
        if roots:
            kept.append((s, f"published at {', '.join(roots)}"))
        elif s.stamp == last:
            kept.append((s, "the last bundle went to it"))
        elif only:
            from mmw.core.repos import repository_label
            names = ", ".join(sorted(repository_label(v.repo, cfg) if cfg else v.repo for v in only))
            kept.append((s, f"holds the version last sent of {names}"))
            doomed.discard(s.id)
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
    if not disk_io.ops.is_dir(d):
        return 0
    have = {s.stamp for s in pool.snapshots(network)}
    imported = 0
    for path in sorted(disk_io.ops.iterdir(d)):
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
