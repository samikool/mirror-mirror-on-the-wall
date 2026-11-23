"""Repository history after successful syncs, with network-selected views.

Repository points freeze accumulated per-unit state. Network recordings link
unchanged slices to their existing point; legacy and layered views retain
the network walk. Publishing and bundling never create repository states.
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


def convert_snapshots(pool: Pool, network: str, warnings: list[str] | None = None) -> int:
    """Once per network: every complete snapshot taken before points, oldest
    first, split into points — linked to the network at the snapshot's own
    stamp and listed for the snapshot. History-only (imported) snapshots
    have no files to split. One that cannot be split is said in `warnings`
    and skipped — it stays a label — so it never stops the network's points.
    How many were converted."""
    if pool.points_converted(network):
        return 0
    done = 0
    for snap in reversed(pool.snapshots(network)):
        if not snap.complete:
            continue
        rows = dict(pool.snapshot_files(snap.id))
        try:
            drafts = split_rows(pool, rows, snap.settings, network, snap.stamp)
        except Exception as e:  # noqa: BLE001 — one old snapshot never blocks the rest
            if warnings is not None:
                warnings.append(f"snapshot {snap.stamp} of {network} could not be split into points ({e}) "
                                "— it stays a label; dates before the next one fall back to it")
            continue
        ids = []
        for repo, draft in drafts:
            pid, _new = pool.record_point(repo, draft, snap.stamp, legacy=True)
            pool.link_network_point(network, pid, snap.stamp)
            ids.append(pid)
        pool.link_snapshot_points(snap.id, ids)
        done += 1
    pool.mark_points_converted(network)
    return done


def seed_recordings(pool: Pool, network: str, now: str | None = None) -> int:
    """Once per network: each complete snapshot's points, per repository, as
    a recording at its stamp, and its settings — so a date before 3c's first
    recording can still be published. When (`now`) is kept: a date before it
    is only as exact as the snapshots. How many snapshots seeded."""
    if pool.recordings_seeded(network):
        return 0
    from mmw.core.publisher.manifest import now_stamp
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
    pool.mark_recordings_seeded(network, (now or now_stamp()) if done else None)
    return done


def record_network_points(network: NetworkConfig, cfg: Config, mirror_dir: Path, *, stamp: str | None = None,
                          progress=None, stop_event=None, after_sync: bool = False) -> PointsReport:
    """Walk `network` and record its points; the ones new to it are said."""
    from mmw.core.publisher.manifest import now_stamp
    stamp = stamp or now_stamp()
    report = PointsReport(network.name)
    with shared_pool_lock(mirror_dir), Pool(mirror_dir) as pool:
        convert_snapshots(pool, network.name, report.warnings)   # their points first, at their own stamps
        seed_recordings(pool, network.name)
        from mmw.core.repository_points import (
            current_repository_points, selected_point_files, current_ref_projection, shared_files,
        )
        from mmw.core.resolve import network_mirrors
        selected = network_mirrors(network, cfg, mirror_dir)
        repository_files = {}
        for repo in {u.source for u in selected}:
            candidates = current_repository_points(pool, repo)
            if not candidates or network.layer_for(repo) is not None:
                continue
            projection = {path: cs for pid in candidates
                          for path, cs in selected_point_files(pool, pid, selected).items()}
            actual = current_ref_projection(pool, [u for u in selected if u.source == repo], shared_files(pool, candidates))
            if projection != actual:
                note = f"{repo}: current files need a complete sync before bundling; retaining the last recorded state"
                if not after_sync:
                    raise ValueError(note)
                report.warnings.append(note)
            repository_files[repo] = projection
        walk_options = {"repository_files": repository_files} if repository_files else {}
        rows, settings, warnings = walk_tree(network, cfg, mirror_dir, pool, progress=progress,
                                             stop_event=stop_event, **walk_options)
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
        from mmw.core.repository_points import current_repository_points, selected_point_files, files_for_settings
        from mmw.core.resolve import network_mirrors
        selected = network_mirrors(network, cfg, mirror_dir)
        for repo, mine in sorted(repos_now.items()):
            digest = content_ident(mine)
            candidates = current_repository_points(pool, repo)
            if candidates and network.layer_for(repo) is None:
                slices = {pid: selected_point_files(pool, pid, selected) for pid in candidates}
                projection = {path: cs for files in slices.values() for path, cs in files.items()}
                if projection != mine:
                    note = f"{repo}: current files need a complete sync before bundling; retaining the last recorded state"
                    if not after_sync:
                        raise ValueError(note)
                    report.warnings.append(note)
                if after_sync and not any(slices.values()):
                    continue
                previous = pool.repo_rows(network.name, repo)
                previous_points = pool.points_of(previous[1] if previous else [])
                for pid, files in list(slices.items()):
                    if not files:
                        continue
                    point = pool.points_of([pid])[pid]
                    unchanged = next((old.id for old in previous_points.values() if old.held and old.track == point.track
                                      and files_for_settings(pool, old.id, settings) == files), None)
                    if unchanged is not None and unchanged != pid:
                        del slices[pid]
                        slices[unchanged] = files
                ids = [pid for pid, files in slices.items() if files]
                for pid in ids:
                    from mmw.core.repository_points import wire_draft
                    wire = wire_draft(pool, pid, slices[pid], settings, stamp)
                    if pool.link_network_point(network.name, pid, stamp):
                        report.new.append(_say(repo, wire, cfg))
                    pool.set_network_slice(network.name, pid, content_ident(slices[pid]), date=wire.date)
                pool.set_repo_rows(network.name, repo, content_ident({p: c for f in slices.values() for p, c in f.items()}), ids)
                if sorted(recorded.get(repo) or []) != sorted(ids):
                    pool.record_recording(network.name, repo, stamp, ids)
                continue
            if after_sync and not repo.startswith("network:") and any(u.source == repo for u in selected) and network.layer_for(repo) is None:
                continue                       # this track has never completed a sync
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
                pid, _new = pool.record_point(repo, draft, stamp, legacy=True)
                ids.append(pid)
                pool.set_network_slice(network.name, pid, content_ident(draft.files))
                if pool.link_network_point(network.name, pid, stamp) and not repo.startswith("network:"):
                    report.new.append(_say(repo, draft, cfg))
            pool.set_repo_rows(network.name, repo, digest, ids)
            if sorted(recorded.get(repo) or []) != sorted(ids):
                pool.record_recording(network.name, repo, stamp, ids)
        pool.record_settings(network.name, stamp, settings)
    return report


def record_points_after_sync(cfg: Config, mirror_dir: Path, synced_sources: set[str],
                             say=print, *, outcomes=None, attempted=None) -> list[PointsReport]:
    """Record the points of every network that selects a source that just
    synced. A network whose walk fails is said and skipped: a sync never
    fails for its points."""
    from mmw.core.resolve import network_mirrors
    from mmw.core.resolve import available_mirrors
    from mmw.core.repos import repository_key
    from mmw.core.repository_points import record_repository, set_current_repository_points, accumulated_refs
    from mmw.core.publisher.manifest import now_stamp
    units = list({u.name: u for u in [*cfg.mirrors, *available_mirrors(cfg, mirror_dir),
                                    *(attempted or [])]}.values())
    by_repo_units = {}
    for unit in units:
        if unit.source in synced_sources and unit.provider in ("apt", "apt-flat", "apt-local"):
            by_repo_units.setdefault(repository_key(unit), []).append(unit)
    successes = {o.mirror for o in outcomes if o.ok} if outcomes is not None else None
    stamp = now_stamp()
    try:
        with shared_pool_lock(mirror_dir), Pool(mirror_dir) as pool:
            requested_all = [u for u in (attempted or units)
                             if outcomes is not None and (attempted is not None or any(o.mirror == u.name for o in outcomes))]
            pool.record_unit_outcomes((repository_key(u), u.dims, u.name in successes) for u in requested_all)
            for repo, mine in by_repo_units.items():
                try:
                    view, mine = accumulated_refs(pool, repo, mine, successes)
                except Exception as e:  # noqa: BLE001 — report history failure without failing the sync
                    say(f"  ⚠ {repo}: repository points not recorded: {e}")
                    continue
                if not mine:
                    continue
                complete = None
                if successes is not None:
                    requested = [u for u in (attempted or units) if repository_key(u) == repo
                                 and (attempted is not None or any(o.mirror == u.name for o in outcomes))]
                    complete = {u.suite for u in mine
                                if all(v.name in successes for v in requested if v.suite == u.suite)}
                    if not complete:
                        continue
                try:
                    recorded = record_repository(pool, repo, mine, mirror_dir, stamp, refs=None if view is pool else view, complete_suites=complete, say=say)
                    set_current_repository_points(pool, repo, recorded, replace=False)
                    new = [_say(repo, d, cfg) for _, created, d in recorded if created]
                    if new:
                        say("New repository point(s) — " + ", ".join(new))
                except Exception as e:  # noqa: BLE001 — history cannot fail the sync
                    say(f"  ⚠ {repo}: repository points not recorded: {e}")
    except Exception as e:  # noqa: BLE001 — recording cannot turn a finished sync into a failure
        say(f"  ⚠ repository points not recorded: {e}")
    reports = []
    for network in cfg.networks:
        try:
            if not any(m.source in synced_sources for m in network_mirrors(network, cfg, mirror_dir)):
                continue
            report = record_network_points(network, cfg, mirror_dir, after_sync=True)
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


def prepare_history(mirror_dir: Path) -> None:
    """Finish retained-history upgrades before the first history/transfer read.

    Schema creation alone isn't completion: an interrupted upgrade can leave
    the table without its marker. Never create a pool or read live refs here;
    the writable open only converts already-retained states, once.
    """
    from mmw.core.pool import database_path
    if not disk_io.ops.exists(database_path(mirror_dir)):
        return
    with Pool(mirror_dir, read_only=True) as pool:
        if pool._has_table('history_upgrades'):
            with pool._read() as conn:
                done = {r[0] for r in conn.execute("SELECT name FROM history_upgrades")}
                if {"manual-snapshots", "legacy-points"} <= done:
                    return
    with shared_pool_lock(mirror_dir), Pool(mirror_dir):
        pass  # The shared storage upgrade converts retained data, not a sync.


def prepare_recordings(mirror_dir: Path, network: str) -> None:
    """Before reading a date: a pool from before recordings gets its tables,
    and existing snapshots seed them — once, without waiting for a sync."""
    prepare_history(mirror_dir)
    with Pool(mirror_dir, read_only=True) as pool:
        try:
            if pool.recordings_seeded(network):
                return
        except sqlite3.OperationalError:
            pass                                   # no tables yet: the writable open makes them
    with shared_pool_lock(mirror_dir), Pool(mirror_dir) as pool:
        convert_snapshots(pool, network)
        seed_recordings(pool, network)


def seeded_note(pool: Pool, network: str, stamp: str) -> str | None:
    """When `stamp` falls before mmw recorded every sync (the snapshots were
    seeded as recordings at the upgrade) and so is read from the snapshot
    before it: what that means. None when the date is recorded as it was,
    or the pool can't say (a read-only pool from before this was kept)."""
    import sqlite3

    from mmw.site.versionref import day
    try:
        if not pool.recordings_seeded(network):
            return None
        at = pool.recordings_seeded_at(network)
        with pool._read() as c:
            stamps = [r[0] for r in c.execute("SELECT DISTINCT stamp FROM network_recordings "
                                              "WHERE network = ? ORDER BY stamp", (network,))]
    except sqlite3.OperationalError:
        return None
    # Seeded recordings sit at the stamps of the snapshots they came from.
    seeded = {s.stamp for s in pool.snapshots(network)
              if s.complete or pool.snapshot_point_ids(s.id)} & set(stamps)
    if at is None:                       # seeded before the moment was kept: its first sync after
        at = next((s for s in stamps if s not in seeded), None)
    if at is not None and stamp >= at:
        return None
    read_from = max((s for s in seeded if s <= stamp), default=None)
    if read_from is None:
        return None
    since = f"since {day(at)}" if at else "from its first sync after the upgrade"
    return (f"{network} on {day(stamp)} is read from its snapshot of {day(read_from)}: mmw records every "
            f"sync only {since} — what synced in between isn't known")


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
    from mmw.core.repository_points import files_for_settings
    settings = pool.settings_at(network, stamp) or "{}"
    files: dict[str, str] = {}
    for pid in ids:
        files.update(files_for_settings(pool, pid, settings))
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


def keep_for(network: NetworkConfig, track) -> int | str:
    """The network's setting for one suite: its `keep_suites` entry (by suite,
    codename, the track's last path part or its whole path), else `keep_points`."""
    names = (track.suite, track.codename, track.name.rsplit("/", 1)[-1] if track.name else None, track.name)
    return next((network.keep_suites[n] for n in names if n and n in network.keep_suites), network.keep_points)


def _kept_by(pool: Pool, network: NetworkConfig, now: str) -> set[int]:
    """The points `network` keeps: per track the newest `keep_points` (or
    those it saw within the age), what it is on now, every point a snapshot
    holds, and — once it has been bundled — every point it first saw after
    its repository was last bundled."""
    by_repo, fallback = history_since(pool, network.name)
    cursors = pool.transfer_dates(network.name)
    sent_points = pool.sent_point_ids(network.name) if cursors else set()
    current = {pid for ids in current_points(pool, network.name).values() for pid in ids}
    # Only a complete snapshot holds: a history-only one stopped holding its files.
    held_by_snapshots = {pid for s in pool.snapshots(network.name) if s.complete
                         for pid in pool.snapshot_point_ids(s.id)}
    published = {pid for p in pool.published(network.name) if p.target.startswith(AS_OF)
                 for ids in pool.points_at(network.name, p.target[len(AS_OF):]).values() for pid in ids}
    kept: set[int] = held_by_snapshots | published
    stamps = pool.network_point_stamps(network.name)
    for t in pool.network_tracks(network.name):
        keep = keep_for(network, t)
        cutoff = _cutoff(now, int(keep[:-1])) if isinstance(keep, str) and keep != "all" else None
        last_sent = by_repo.get(t.repo, fallback)
        for i, p in enumerate(pool.network_points(network.name, t.id)):
            first, seen = stamps.get(p.id, (None, None))
            first = first or p.stamp
            # An age counts from when the network last had it; what it's on now always stays.
            seen = seen or first
            if (i == 0 or p.id in current or keep == "all" or (isinstance(keep, int) and i < keep)
                    or (cutoff is not None and seen >= cutoff)
                    or p.id in held_by_snapshots or p.id in published or (last_sent is not None and first > last_sent)
                    or (t.id in cursors and p.id not in sent_points
                        and (pool.network_slice_date(network.name, p.id) or p.date) > cursors[t.id])):
                kept.add(p.id)
    return kept


def superseded_at(points_newest_first, now=None) -> dict[int, str]:
    """When each state stopped being current, independently for each track."""
    from mmw.core.publisher.manifest import now_stamp
    now = now or now_stamp()
    newer, ends = {}, {}
    for point in points_newest_first:
        ends[point.id] = newer.get(point.track, now)
        newer[point.track] = point.stamp
    return ends


def orphaned_histories(pool, cfg) -> list[tuple[str, int]]:
    """Removed repositories whose entire held history the prune releases."""
    configured = {s.identity for s in cfg.sources}
    gone = set(retention_plan(pool, cfg.networks, cfg=cfg))
    by_repo = {}
    for point in pool.repository_points():
        repo = pool.track_of_point(point.id).repo
        by_repo.setdefault(repo, set()).add(point.id)
    return sorted((repo, len(ids)) for repo, ids in by_repo.items()
                  if repo not in configured and ids <= gone)


def retention_plan(pool: Pool, networks: list[NetworkConfig], *, now: str | None = None, cfg: Config | None = None) -> list[int]:
    """The held points no configured network keeps any more (none dropped)."""
    from mmw.core.publisher.manifest import now_stamp
    now = now or now_stamp()
    kept: set[int] = set()
    for n in networks:
        kept |= _kept_by(pool, n, now)
    kept |= pool.published_point_ids()
    # Every held point any network saw: one only a removed network saw is kept by none.
    candidates = set(pool.linked_held_points())
    if cfg is not None:
        from types import SimpleNamespace
        from mmw.core.repository_points import current_repository_points
        # Modern history is owned by repositories. Network caches and old
        # network retention rules must not pin it indefinitely.
        owned = {}
        by_repo_track = {}
        for p in pool.repository_points():
            track = pool.track_of_point(p.id)
            owned[p.id] = track.repo
            by_repo_track.setdefault((track.repo, p.track), []).append(p)
        candidates.update(owned)
        kept.difference_update(owned)
        # A history network still needs points observed since its last transfer.
        for n in networks:
            if not n.history:
                continue
            by_repo, fallback = history_since(pool, n.name)
            if fallback is None:
                continue
            sent = pool.sent_point_ids(n.name)
            for pid, (first, _) in pool.network_point_stamps(n.name).items():
                if pid in owned and pid not in sent and first > by_repo.get(owned[pid], fallback):
                    kept.add(pid)
        kept |= pool.published_point_ids()
        # Before publish destinations recorded exact point IDs, an as-of
        # deployment was identified by its saved network recording instead.
        kept |= {pid for n in networks for deployment in pool.published(n.name)
                 if deployment.target.startswith(AS_OF)
                 for ids in pool.points_at(n.name, deployment.target[len(AS_OF):]).values()
                 for pid in ids if pid in owned}
        # Explicit old snapshots remain compatibility roots until migrated.
        kept |= {pid for n in networks for snap in pool.snapshots(n.name) if snap.complete
                 for pid in pool.snapshot_point_ids(snap.id) if pid in owned}
        for (repo, _), ps in by_repo_track.items():
            source = next((s for s in cfg.sources if s.identity == repo), None)
            if source is None:
                continue
            own = getattr(source, "keep_points", None)
            rule = SimpleNamespace(keep_points=cfg.settings.keep_points if own is None else own,
                                   keep_suites=getattr(source, "keep_suites", {}))
            keep = keep_for(rule, pool.track_of_point(ps[0].id))
            cutoff = _cutoff(now, int(keep[:-1])) if isinstance(keep, str) and keep != "all" else None
            current = set(current_repository_points(pool, repo))
            ends = superseded_at(ps, now)
            for i, p in enumerate(ps):
                if (i == 0 or p.id in current or keep == "all" or (isinstance(keep, int) and i < keep)
                        or (cutoff is not None and ends[p.id] >= cutoff)):
                    kept.add(p.id)
    return sorted(candidates - kept)


def apply_retention(pool: Pool, networks: list[NetworkConfig], *, now: str | None = None, cfg: Config | None = None) -> int:
    """Stub every point `retention_plan` names; how many."""
    gone = retention_plan(pool, networks, now=now, cfg=cfg)
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
