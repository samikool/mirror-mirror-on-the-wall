"""Every sync is a point: after a sync, each network's tree (as a snapshot
would record it) split per repository and per Release into points.

A point is one Release of one repository with one file set, stored once and
linked to every network that saw it. A suite that didn't change adds
nothing; files no repository claims are the network's own `network` track.
"""
from __future__ import annotations

import sqlite3
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
    settings: str = "{}"                                # the walk's (what nginx is generated from)


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


def seed_recordings(pool: Pool, network: str) -> int:
    """Once per network: each complete snapshot's points, per repository, as
    a recording at its stamp, and its settings — so a date before 3c's first
    recording can still be published. How many snapshots seeded."""
    if pool.recordings_seeded(network):
        return 0
    done = 0
    for snap in reversed(pool.snapshots(network)):
        if not snap.complete:
            continue
        ids = pool.snapshot_point_ids(snap.id)
        if not ids:
            continue
        by: dict[str, list[int]] = {}
        for pid in ids:
            t = pool.track_of_point(pid)
            if t is not None:
                by.setdefault(t.repo, []).append(pid)
        last = pool.points_at(network, snap.stamp)
        for repo, pids in by.items():
            if sorted(last.get(repo, [])) != sorted(pids):
                pool.record_recording(network, repo, snap.stamp, pids)
        for repo in set(last) - set(by):
            pool.record_recording(network, repo, snap.stamp, [])
        if snap.settings:
            pool.record_settings(network, snap.stamp, snap.settings)
        done += 1
    pool.mark_recordings_seeded(network)
    return done


def record_network_points(network: NetworkConfig, cfg: Config, mirror_dir: Path, *, stamp: str | None = None,
                          progress=None, stop_event=None) -> PointsReport:
    """Walk `network` and record its points; the ones new to it are said."""
    from mmw.core.publisher.manifest import now_stamp
    stamp = stamp or now_stamp()
    report = PointsReport(network.name)
    with shared_pool_lock(mirror_dir), Pool(mirror_dir) as pool:
        convert_snapshots(pool, network.name)      # their points first, at their own stamps
        seed_recordings(pool, network.name)
        rows, settings, warnings = walk_tree(network, cfg, mirror_dir, pool, progress=progress,
                                             stop_event=stop_event)
        report.warnings += warnings
        report.settings = settings
        from mmw.site.tracks import content_ident
        recorded = pool.latest_recordings(network.name)
        repos_now = by_repo(rows, settings, network.name)
        for repo in set(pool.recorded_repos(network.name)) - set(repos_now):
            pool.drop_repo_rows(network.name, repo)             # it left the network
        for repo, ids in recorded.items():
            if ids and repo not in repos_now:
                pool.record_recording(network.name, repo, stamp, [])
        for repo, mine in sorted(repos_now.items()):
            digest = content_ident(mine)
            last = pool.repo_rows(network.name, repo)
            if last is not None and last[0] == digest:
                # Unchanged since the last recording: its points are seen again, nothing is split.
                for pid in last[1]:
                    pool.link_network_point(network.name, pid, stamp)
                if sorted(recorded.get(repo) or []) != sorted(last[1]):
                    pool.record_recording(network.name, repo, stamp, last[1])
                continue
            ids = []
            for draft in split_repo(pool, repo, mine, settings, stamp):
                pid, _new = pool.record_point(repo, draft, stamp)
                ids.append(pid)
                if pool.link_network_point(network.name, pid, stamp) and not repo.startswith("network:"):
                    report.new.append(_say(repo, draft, cfg))
            pool.set_repo_rows(network.name, repo, digest, ids)
            if sorted(recorded.get(repo) or []) != sorted(ids):
                pool.record_recording(network.name, repo, stamp, ids)
        pool.record_settings(network.name, stamp, settings)
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


# ── as of a date ─────────────────────────────────────────────────────────────

AS_OF = "as-of:"          # a published record's target for a date (else 'live' or a snapshot stamp)


def resolve_when(text: str) -> str:
    """A date or stamp (`2026-11-03`, `20261103`, `3nov`, a full stamp) as
    the stamp it means: a bare day is the whole of it (UTC)."""
    from datetime import datetime, timezone
    from mmw.site.versionref import _date_prefix
    dated = _date_prefix(text.strip(), datetime.now(timezone.utc).date())
    if dated is None:
        raise ValueError(f"{text!r} isn't a date or stamp")
    prefix = dated[0]
    if len(prefix) == 8:
        return f"{prefix}T235959Z"
    day_part, time_part = prefix[:8], prefix[9:].rstrip("Z")
    return f"{day_part}T{time_part + '235959'[len(time_part):]}Z"     # the end of what it names


def prepare_recordings(mirror_dir: Path, network: str) -> None:
    """Before reading a date: a pool from before recordings gets its tables,
    and existing snapshots seed them — once, without waiting for a sync."""
    with Pool(mirror_dir, read_only=True) as pool:
        try:
            if pool.recordings_seeded(network):
                return
        except sqlite3.OperationalError:
            pass                                   # no tables yet: the writable open makes them
    with shared_pool_lock(mirror_dir), Pool(mirror_dir) as pool:
        convert_snapshots(pool, network)
        seed_recordings(pool, network)


def moment_rows(pool: Pool, network: str, stamp: str):
    """(rows, settings) of `network` as recorded at `stamp`: rows are
    (path, checksum, pool path, size) in path order. Refused when nothing was
    recorded by then, or the moment's points or files are gone."""
    from mmw.site.versionref import day
    held = pool.points_at(network, stamp)
    if not held:
        first = pool.first_recording(network)
        if first is not None and first <= stamp:
            raise ValueError(f"no repository was in {network} on {day(stamp)}")
        raise ValueError(f"nothing recorded for {network} at or before {day(stamp)}"
                         + (f" — the first recording is {day(first)}" if first else
                            " — nothing is recorded until it syncs"))
    ids = [pid for pids in held.values() for pid in pids]
    stubs = [p for p in pool.points_of(ids).values() if not p.held]
    if stubs:
        raise ValueError(f"{len(stubs)} point(s) {network} had on {day(stamp)} are no longer kept "
                         f"(retention dropped their files) — raise keep_points on {network} to keep "
                         "older dates, or publish a later one")
    files: dict[str, str] = {}
    for pid in ids:
        files.update(pool.point_files(pid))
    info = pool.blob_info(files.values())
    missing = sorted({c for c in files.values() if c not in info})
    if missing:
        raise ValueError(f"{len(missing)} file(s) of {network} on {day(stamp)} are no longer in the pool "
                         "— `mmw verify --repair`, or publish another date")
    rows = [(rel, cs, info[cs][0], info[cs][1]) for rel, cs in sorted(files.items())]
    return rows, pool.settings_at(network, stamp) or "{}"


# ── retention ────────────────────────────────────────────────────────────────

def _cutoff(now: str, days: int) -> str:
    from datetime import datetime, timedelta, timezone
    t = datetime.strptime(now, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) - timedelta(days=days)
    return t.strftime("%Y%m%dT%H%M%SZ")


def history_since(pool: Pool, network: str) -> tuple[dict[str, str], str | None]:
    """Where a bundle's history starts, per repository: the last bundle that
    listed it — a partial bundle moves only its own repositories on. A
    repository no bundle listed yet starts at the network's last bundle
    (None: never bundled). Returns (per repository, that fallback)."""
    bundles = pool.bundles(network)
    return pool.last_sent_by_repo(network), (bundles[0].to_stamp if bundles else None)


def _kept_by(pool: Pool, network: NetworkConfig, now: str) -> set[int]:
    """The points `network` keeps: per track the newest `keep_points` (or
    those it saw within the age), what it is on now, every point a snapshot
    holds, and — once it has been bundled — every point it first saw after
    its repository was last bundled."""
    keep = network.keep_points
    by_repo, fallback = history_since(pool, network.name)
    current = {pid for ids in current_points(pool, network.name).values() for pid in ids}
    # Only a complete snapshot holds: a history-only one stopped holding its files.
    held_by_snapshots = {pid for s in pool.snapshots(network.name) if s.complete
                         for pid in pool.snapshot_point_ids(s.id)}
    published = {pid for p in pool.published(network.name) if p.target.startswith(AS_OF)
                 for ids in pool.points_at(network.name, p.target[len(AS_OF):]).values() for pid in ids}
    cutoff = _cutoff(now, int(keep[:-1])) if isinstance(keep, str) and keep != "all" else None
    kept: set[int] = set()
    for t in pool.network_tracks(network.name):
        last_sent = by_repo.get(t.repo, fallback)
        for i, p in enumerate(pool.network_points(network.name, t.id)):
            first = pool.network_point_stamp(network.name, p.id) or p.stamp
            # An age counts from when the network last had it; what it's on now always stays.
            seen = pool.network_point_last_seen(network.name, p.id) or first
            if (i == 0 or p.id in current or keep == "all" or (isinstance(keep, int) and i < keep)
                    or (cutoff is not None and seen >= cutoff)
                    or p.id in held_by_snapshots or p.id in published or (last_sent is not None and first > last_sent)):
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


# ── what each network was sent ───────────────────────────────────────────────

def current_points(pool: Pool, network: str) -> dict[str, list[int]]:
    """Repository → the points `network` had at its last recording."""
    return pool.recorded_repos(network)


def migrate_sent(pool: Pool, network: str) -> bool:
    """Once per network: what its format-2 bundles sent (each repository's
    last-sent version) as sent points and files — so the first bundle of
    points is still a delta. Whether it ran."""
    if pool.sent_migrated(network):
        return False
    convert_snapshots(pool, network)
    if pool.full_next(network) is not None:
        # A forced delete said the far side's state is unknown: its history
        # isn't trusted — nothing counts as sent, so the first bundle is full.
        pool.clear_full_next(network)
        pool.mark_sent_migrated(network)
        return True
    for repo, version_id in pool.last_sent_versions(network).items():
        holders = pool.repo_version_snapshots(version_id)
        if not holders:
            continue                    # its snapshot is gone: those files go again (the far side dedupes)
        ids = [pid for pid in pool.snapshot_point_ids(holders[0])
               if (t := pool.track_of_point(pid)) is not None and t.repo == repo]
        files = {c for pid in ids for c in pool.point_files(pid).values()}
        pool.record_sent(network, ids, files, "migrated")
    pool.mark_sent_migrated(network)
    return True
