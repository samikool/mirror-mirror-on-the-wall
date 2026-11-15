"""Every sync is a point: after a sync, each network's tree (as a snapshot
would record it) split per repository and per Release into points.

A point is one Release of one repository with one file set, stored once and
linked to every network that saw it. A suite that didn't change adds
nothing; files no repository claims are the network's own `network` track.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from mmw.core import disk_io
from mmw.core.config import Config, NetworkConfig
from mmw.core.pool import Pool, shared_pool_lock
from mmw.core.snapshots import recorded_repos, walk_tree


@dataclass
class PointsReport:
    network: str
    new: list[str] = field(default_factory=list)        # "a stable 2026-11-01"
    warnings: list[str] = field(default_factory=list)


def _reader(pool: Pool, rows: dict[str, str]):
    def read(path: str) -> bytes | None:
        cs = rows.get(path)
        f = pool.blob_file(cs) if cs else None
        if f is None:
            return None
        try:
            return disk_io.ops.read_bytes(f)
        except OSError:
            return None
    return read


def by_repo(rows: dict[str, str], settings: str, network: str) -> dict[str, dict[str, str]]:
    """A network's rows per repository; those of none under `network:{name}`."""
    from mmw.core.repos import attribute
    repos = recorded_repos(settings)
    out: dict[str, dict[str, str]] = {}
    for path, cs in rows.items():
        repo = attribute(path, repos)
        out.setdefault(repo if repo is not None else f"network:{network}", {})[path] = cs
    return out


def split_repo(pool: Pool, repo: str, rows: dict[str, str], settings: str, stamp: str) -> list:
    """One repository's PointDrafts (a `network:` one is a single point)."""
    from mmw.site import tracks
    if repo.startswith("network:"):
        return [tracks.PointDraft("network", tracks.content_ident(rows), stamp, {}, rows)]
    return tracks.split_tracks(rows, recorded_repos(settings)[repo], _reader(pool, rows), stamp)


def split_rows(pool: Pool, rows: dict[str, str], settings: str, network: str, stamp: str) -> list[tuple[str, object]]:
    """(repo, PointDraft) for every point in a network's rows; the rows of no
    repository are one `network` track of `network:{name}`."""
    return [(repo, d) for repo, mine in sorted(by_repo(rows, settings, network).items())
            for d in split_repo(pool, repo, mine, settings, stamp)]


def _say(repo: str, draft, cfg: Config) -> str:
    from mmw.core.repos import repository_label
    from mmw.site.versionref import day
    label = repo.split(":", 1)[1] + " (network)" if repo.startswith("network:") else repository_label(repo, cfg)
    suite = draft.fields.get("Suite") or (draft.track.rsplit("/", 1)[-1] if draft.track else "files")
    return f"{label} {suite} {day(draft.date)}"


def convert_snapshots(pool: Pool, network: str) -> int:
    """Once per network: every complete snapshot taken before points, oldest
    first, split into points — linked to the network at the snapshot's own
    stamp and listed for the snapshot. History-only (imported) snapshots
    have no files to split. How many were converted."""
    if pool.points_converted(network):
        return 0
    done = 0
    for snap in reversed(pool.snapshots(network)):
        if not snap.complete:
            continue
        rows = dict(pool.snapshot_files(snap.id))
        ids = []
        for repo, draft in split_rows(pool, rows, snap.settings, network, snap.stamp):
            pid, _new = pool.record_point(repo, draft, snap.stamp)
            pool.link_network_point(network, pid, snap.stamp)
            ids.append(pid)
        pool.link_snapshot_points(snap.id, ids)
        done += 1
    pool.mark_points_converted(network)
    return done


def record_network_points(network: NetworkConfig, cfg: Config, mirror_dir: Path, *, stamp: str | None = None,
                          progress=None, stop_event=None) -> PointsReport:
    """Walk `network` and record its points; the ones new to it are said."""
    from mmw.core.publisher.manifest import now_stamp
    stamp = stamp or now_stamp()
    report = PointsReport(network.name)
    with shared_pool_lock(mirror_dir), Pool(mirror_dir) as pool:
        convert_snapshots(pool, network.name)      # their points first, at their own stamps
        rows, settings, warnings = walk_tree(network, cfg, mirror_dir, pool, progress=progress,
                                             stop_event=stop_event)
        report.warnings += warnings
        from mmw.site.tracks import content_ident
        for repo, mine in sorted(by_repo(rows, settings, network.name).items()):
            digest = content_ident(mine)
            last = pool.repo_rows(network.name, repo)
            if last is not None and last[0] == digest:
                # Unchanged since the last recording: its points are seen again, nothing is split.
                for pid in last[1]:
                    pool.link_network_point(network.name, pid, stamp)
                continue
            ids = []
            for draft in split_repo(pool, repo, mine, settings, stamp):
                pid, _new = pool.record_point(repo, draft, stamp)
                ids.append(pid)
                if pool.link_network_point(network.name, pid, stamp) and not repo.startswith("network:"):
                    report.new.append(_say(repo, draft, cfg))
            pool.set_repo_rows(network.name, repo, digest, ids)
    return report


def record_points_after_sync(cfg: Config, mirror_dir: Path, synced_sources: set[str],
                             say=print) -> list[PointsReport]:
    """Record the points of every network that selects a source that just
    synced. A network whose walk fails is said and skipped: a sync never
    fails for its points."""
    from mmw.core.resolve import network_mirrors
    reports = []
    for network in cfg.networks:
        try:
            if not any(m.source in synced_sources for m in network_mirrors(network, cfg, mirror_dir)):
                continue
            report = record_network_points(network, cfg, mirror_dir)
        except Exception as e:  # noqa: BLE001 — said, and the sync goes on
            report = PointsReport(network.name, warnings=[f"points not recorded: {e}"])
        for w in report.warnings:
            say(f"  ⚠ {network.name}: {w}")
        if report.new:
            say(f"{network.name}: new point(s) — {', '.join(report.new)}")
        reports.append(report)
    return reports


# ── retention ────────────────────────────────────────────────────────────────

def _cutoff(now: str, days: int) -> str:
    from datetime import datetime, timedelta, timezone
    t = datetime.strptime(now, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) - timedelta(days=days)
    return t.strftime("%Y%m%dT%H%M%SZ")


def _kept_by(pool: Pool, network: NetworkConfig, now: str) -> set[int]:
    """The points `network` keeps: per track the newest `keep_points` (or
    those it saw within the age), every point a snapshot holds, and — once
    it has been bundled — every point it first saw after its last bundle."""
    keep = network.keep_points
    bundles = pool.bundles(network.name)
    last_sent = bundles[0].to_stamp if bundles else None
    # Only a complete snapshot holds: a history-only one stopped holding its files.
    held_by_snapshots = {pid for s in pool.snapshots(network.name) if s.complete
                         for pid in pool.snapshot_point_ids(s.id)}
    cutoff = _cutoff(now, int(keep[:-1])) if isinstance(keep, str) and keep != "all" else None
    kept: set[int] = set()
    for t in pool.network_tracks(network.name):
        for i, p in enumerate(pool.network_points(network.name, t.id)):
            first = pool.network_point_stamp(network.name, p.id) or p.stamp
            # An age counts from when the network last had it; what it's on now always stays.
            seen = pool.network_point_last_seen(network.name, p.id) or first
            if (i == 0 or keep == "all" or (isinstance(keep, int) and i < keep)
                    or (cutoff is not None and seen >= cutoff)
                    or p.id in held_by_snapshots or (last_sent is not None and first > last_sent)):
                kept.add(p.id)
    return kept


def retention_plan(pool: Pool, networks: list[NetworkConfig], *, now: str | None = None) -> list[int]:
    """The held points no configured network keeps any more (none dropped)."""
    from mmw.core.publisher.manifest import now_stamp
    now = now or now_stamp()
    kept: set[int] = set()
    for n in networks:
        kept |= _kept_by(pool, n, now)
    # Every held point any network saw: one only a removed network saw is kept by none.
    return sorted(set(pool.linked_held_points()) - kept)


def apply_retention(pool: Pool, networks: list[NetworkConfig], *, now: str | None = None) -> int:
    """Stub every point `retention_plan` names; how many."""
    gone = retention_plan(pool, networks, now=now)
    for pid in gone:
        pool.stub_point(pid)
    return len(gone)
