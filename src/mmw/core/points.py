"""Repository history after successful syncs, with network-selected views.

Repository points freeze accumulated per-unit state. Network recordings link
each repository's current points (or their layer views); only the network's
own track comes from a walk. Publishing and bundling never create repository
states.
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
    walked: dict[str, list[int]] = field(default_factory=dict)   # repository → its points from the walk


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


def partial_sync_note(pool, repo, current, recorded, now) -> str:
    """What a sync left half done, said plainly: the architectures (or
    components) whose indexes don't match the suite's Release now, and the
    date the suite stays at until they sync — "arm64 didn't sync — jammy
    stays at 2022-04-21 until it does"."""
    import re
    from mmw.core.point_check import check_suite
    from mmw.core.repository_points import _blob_reader
    from mmw.site.versionref import day
    blob = _blob_reader(pool)
    changed = {p for p in recorded.keys() | now.keys() if recorded.get(p) != now.get(p)}
    tracks = {}
    for point in pool.points_of(current).values():
        track = pool.track_of_point(point.id)
        if track is not None and track.name and '/dists/' in track.name:
            tracks[track.name] = point
    notes = []
    for name, point in sorted(tracks.items()):
        if not any(p.startswith(name + '/') for p in changed):
            continue
        suite, date = name.rsplit('/dists/', 1)[1], day(point.date)
        problems = check_suite(now, name, lambda path: blob(now[path]) if path in now else None)
        behind = set()
        for problem in problems:
            arch = re.search(r'(?:binary|Contents|installer|Components)-([A-Za-z0-9_]+)', problem)
            if arch:
                behind.add(arch.group(1))
            elif problem.endswith(' is missing'):
                behind.add('packages')            # an index names a file that never arrived
            else:
                behind.add(problem.split('/', 1)[0].split(' ', 1)[0])
        if behind:
            notes.append(f"{repo}: {', '.join(sorted(behind))} didn't sync — {suite} stays at {date} until it does")
        else:
            notes.append(f"{repo}: {suite} changed without a new point — it stays at {date} until a sync completes")
    return '; '.join(notes) or f"{repo}: files changed without a new point — it stays at its last point until a sync completes"


def record_network_points(network: NetworkConfig, cfg: Config, mirror_dir: Path, *, stamp: str | None = None,
                          progress=None, stop_event=None, after_sync: bool = False,
                          fresh=frozenset()) -> PointsReport:
    """Record `network`'s points; the ones new to it are said. Each
    repository's current points (a layered one's: their views, built first)
    are linked with their slice digests and the recording. A walk records
    only the network's own track (keys, PyPI's pages, the gallery) — and a
    repository no sync ever recorded a point of, whose refs are all it has.
    `fresh`: repository points the sync just recorded (their layer views
    serve from the point's date)."""
    from mmw.core.publisher.manifest import now_stamp
    stamp = stamp or now_stamp()
    report = PointsReport(network.name)
    with shared_pool_lock(mirror_dir), Pool(mirror_dir) as pool:
        from mmw.core.repository_points import (
            current_repository_points, selected_point_files, current_ref_projection, shared_files,
            provider_scoped, provider_view, files_for_settings, wire_draft,
        )
        from mmw.core.repos import repository_key
        from mmw.core.resolve import network_mirrors
        from mmw.core.layers import build_network_layers, served_files
        from mmw.site.tracks import content_ident
        # A layer is a view of each current point: built (or found unchanged)
        # before the points are linked, so the network links what it serves.
        layered = build_network_layers(network, cfg, mirror_dir, pool, stamp=stamp, fresh=fresh)
        report.warnings += layered.warnings
        if layered.errors:
            if not after_sync:
                raise ValueError(f"points of {network.name} not recorded: " + "; ".join(layered.errors[:5]))
            report.warnings += layered.errors
        selected = network_mirrors(network, cfg, mirror_dir)
        repository_files, point_views, slices_of = {}, {}, {}
        for repo in sorted({repository_key(u) for u in selected}):
            candidates = current_repository_points(pool, repo)
            if not candidates:
                continue                          # see below: its recording, or (never synced) its refs
            # The network's slice of each point: a layer's view, a provider's publish.
            slices = {pid: selected_point_files(pool, pid, selected, network) for pid in candidates}
            if provider_scoped(repo):
                view = provider_view(pool, repo, candidates, units=selected, network=network)
                if view is not None:
                    point_views[repo] = view      # its pages are published from the point too
            else:
                plain = {path: cs for pid in candidates
                         for path, cs in selected_point_files(pool, pid, selected, network.name).items()}
                actual = current_ref_projection(pool, [u for u in selected if u.source == repo],
                                                shared_files(pool, candidates))
                if plain != actual:
                    report.warnings.append(partial_sync_note(pool, repo, candidates, plain, actual))
            repository_files[repo] = {p: cs for files in slices.values() for p, cs in files.items()}
            slices_of[repo] = slices
        for repo, files in served_files(pool, network, cfg, mirror_dir).items():
            repository_files.setdefault(repo, files)     # points none is current of yet: as their views
        walk_options = {"repository_files": repository_files, "point_views": point_views} if repository_files else {}
        rows, settings, warnings = walk_tree(network, cfg, mirror_dir, pool, progress=progress,
                                             stop_event=stop_event, **walk_options)
        report.warnings += warnings
        report.settings = settings
        recorded = pool.latest_recordings(network.name)
        net_repo = f"network:{network.name}"
        repos_now = by_repo(rows, settings, network.name)
        for repo, ids in recorded.items():
            if ids and repo not in repos_now and repo not in slices_of:
                pool.record_recording(network.name, repo, stamp, [])
        for repo, slices in sorted(slices_of.items()):
            if after_sync and not any(slices.values()):
                continue
            # A point whose slice is one already recorded of its track is that point.
            previous = pool.points_of(recorded.get(repo) or [])
            for pid, files in list(slices.items()):
                if not files:
                    continue
                point = pool.points_of([pid])[pid]
                unchanged = next((old.id for old in previous.values() if old.held and old.track == point.track
                                  and files_for_settings(pool, old.id, settings) == files), None)
                if unchanged is not None and unchanged != pid:
                    del slices[pid]
                    slices[unchanged] = files
            ids = [pid for pid, files in slices.items() if files]
            for pid in ids:
                wire = wire_draft(pool, pid, slices[pid], settings, stamp)
                if pool.link_network_point(network.name, pid, stamp):
                    report.new.append(_say(repo, wire, cfg))
                pool.set_network_slice(network.name, pid, content_ident(slices[pid]), date=wire.date)
            if sorted(recorded.get(repo) or []) != sorted(ids):
                pool.record_recording(network.name, repo, stamp, ids)
        for repo, mine in sorted(repos_now.items()):
            if repo in slices_of:
                continue
            if repo != net_repo and pool.repository_points(repo, stubs=True):
                continue                  # no current point: its recording stands, its points are read
            if after_sync and repo != net_repo and any(u.source == repo for u in selected):
                continue                  # this track has never completed a sync
            # The network's own track, and a repository no sync ever recorded
            # (its refs are all there is): what the walk laid out.
            last = recorded.get(repo) or []
            if last and content_ident({p: c for pid in last for p, c in pool.point_files(pid).items()}) \
                    == content_ident(mine):
                for pid in last:               # unchanged since the last recording: seen again
                    pool.link_network_point(network.name, pid, stamp)
                if repo != net_repo:
                    report.walked[repo] = list(last)
                continue
            ids = []
            for draft in split_repo(pool, repo, mine, settings, stamp):
                pid, _new = pool.record_point(repo, draft, stamp, scope=network.name)
                ids.append(pid)
                pool.set_network_slice(network.name, pid, content_ident(draft.files))
                if pool.link_network_point(network.name, pid, stamp) and repo != net_repo:
                    report.new.append(_say(repo, draft, cfg))
            if sorted(last) != sorted(ids):
                pool.record_recording(network.name, repo, stamp, ids)
            if repo != net_repo:
                report.walked[repo] = ids
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
    from mmw.core.repository_points import (
        record_repository, set_current_repository_points, set_repository_synced, accumulated_refs, coordinates, provider_scoped,
    )
    from mmw.core.publisher.manifest import now_stamp
    units = list({u.name: u for u in [*cfg.mirrors, *available_mirrors(cfg, mirror_dir),
                                    *(attempted or [])]}.values())
    by_repo_units = {}
    for unit in units:
        if unit.source in synced_sources:
            by_repo_units.setdefault(repository_key(unit), []).append(unit)
    successes = {o.mirror for o in outcomes if o.ok} if outcomes is not None else None
    stamp = now_stamp()
    fresh = set()                       # this sync's points: a layer's first view serves from their date
    try:
        with shared_pool_lock(mirror_dir), Pool(mirror_dir) as pool:
            requested_all = [u for u in (attempted or units)
                             if outcomes is not None and (attempted is not None or any(o.mirror == u.name for o in outcomes))]
            pool.record_unit_outcomes((repository_key(u), coordinates(u), u.name in successes) for u in requested_all)
            for repo, mine in by_repo_units.items():
                try:
                    view, mine = accumulated_refs(pool, repo, mine, successes)
                except Exception as e:  # noqa: BLE001 — report history failure without failing the sync
                    say(f"  ⚠ {repo}: repository points not recorded: {e}")
                    continue
                if not mine:
                    continue
                complete = None
                # A content track has no suites to complete: a failed unit
                # keeps its last recorded files (accumulated_refs).
                if successes is not None and not provider_scoped(repo):
                    requested = [u for u in (attempted or units) if repository_key(u) == repo
                                 and (attempted is not None or any(o.mirror == u.name for o in outcomes))]
                    complete = {u.suite for u in mine
                                if all(v.name in successes for v in requested if v.suite == u.suite)}
                    if not complete:
                        continue
                try:
                    recorded = record_repository(pool, repo, mine, mirror_dir, stamp, refs=None if view is pool else view, complete_suites=complete, say=say)
                    set_current_repository_points(pool, repo, recorded, replace=False)
                    set_repository_synced(pool, repo, stamp)
                    fresh.update(pid for pid, _, _ in recorded)
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
            report = record_network_points(network, cfg, mirror_dir, after_sync=True, fresh=frozenset(fresh))
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


CONVERTING = "Converting history from an older mmw…"


def prepare_history(mirror_dir: Path, say=None) -> bool:
    """Convert every older history in the pool (the upgrade chain,
    `legacy_points.upgrade`) before anything reads it: once, at startup.
    `say` hears `CONVERTING` only when there is something to convert.
    Whether it converted anything. Never creates a pool."""
    from mmw.core.legacy_points import CHAIN_DONE, done
    from mmw.core.pool import database_path
    if not disk_io.ops.exists(database_path(mirror_dir)):
        return False
    with Pool(mirror_dir, read_only=True) as pool:
        if CHAIN_DONE <= done(pool):
            return False
    if say is not None:
        say(CONVERTING)
    with shared_pool_lock(mirror_dir), Pool(mirror_dir):
        pass  # opening it writable runs the chain
    return True


def seeded_note(pool: Pool, network: str, stamp: str) -> str | None:
    """When `stamp` falls before mmw recorded every sync (the snapshots were
    seeded as recordings at the upgrade) and so is read from the snapshot
    before it: what that means. None when the date is recorded as it was,
    or the pool can't say (a read-only pool from before this was kept)."""

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


def moment_problem(pool: Pool, network: str, stamp: str) -> str | None:
    """Why `network`'s recordings can't give it as at `stamp`, said for a
    person: nothing recorded by then, no repository then, or its points no
    longer kept. None when they can."""
    from mmw.site.versionref import day
    held = pool.points_at(network, stamp)
    if not held:
        first = pool.first_recording(network)
        if first is not None and first <= stamp:
            return f"no repository was in {network} on {day(stamp)}"
        return (f"nothing recorded for {network} at or before {day(stamp)}"
                + (f" — the first recording is {day(first)}" if first else
                   " — nothing is recorded until it syncs"))
    ids = [pid for pids in held.values() for pid in pids]
    stubs = [p for p in pool.points_of(ids).values() if not p.held]
    if stubs:
        return (f"{len(stubs)} point(s) {network} had on {day(stamp)} are no longer kept "
                f"(retention dropped their files) — raise the repositories' keep_points "
                "(`mmw keep`) to keep older dates, or publish a later one")
    return None


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


def keep_for(rule, track) -> int | str:
    """A repository's setting for one suite: its `keep_suites` entry (by
    suite, codename, the track's last path part or its whole path), else its
    `keep_points`. `rule` is a source with both resolved."""
    names = (track.suite, track.codename, track.name.rsplit("/", 1)[-1] if track.name else None, track.name)
    return next((rule.keep_suites[n] for n in names if n and n in rule.keep_suites), rule.keep_points)


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


def unsent_points(pool: Pool, network: NetworkConfig, owned: dict[int, str] | None = None) -> set[int]:
    """The points a `history = true` network observed since its last
    transfer and has not been sent: its next bundle still needs them.
    `owned`: point → repository (every repository point when omitted)."""
    if not network.history:
        return set()
    by_repo, fallback = history_since(pool, network.name)
    if fallback is None:
        return set()
    if owned is None:
        owned = {p.id: pool.track_of_point(p.id).repo for p in pool.repository_points()}
    sent = pool.sent_point_ids(network.name)
    linked = {pid: first for pid, (first, _) in pool.network_point_stamps(network.name).items()
              if pid in owned and pid not in sent}
    # Newer than what its track was sent (a bundle `--until` an earlier date
    # leaves the later points due, though they were seen before it was made).
    newest_sent: dict[int, tuple[str, str]] = {}
    for p in pool.points_of(sorted(sent)).values():
        newest_sent[p.track] = max(newest_sent.get(p.track, ("", "")), (p.date, p.stamp))
    mine = pool.points_of(sorted(linked))
    return {pid for pid, first in linked.items()
            if first > by_repo.get(owned[pid], fallback)
            or (pid in mine and mine[pid].track in newest_sent
                and (mine[pid].date, mine[pid].stamp) > newest_sent[mine[pid].track])}


def retention_plan(pool: Pool, networks: list[NetworkConfig], *, now: str | None = None, cfg: Config) -> list[int]:
    """The held points no configured network keeps any more (none dropped).
    Every track follows a rule: a repository's points (and a network's own
    points of it, `PoolPoint.scoped`) their source's `keep_points` /
    `keep_suites`; a network's own track (`network:NAME`) the global
    `keep_points`. A removed repository or network keeps nothing."""
    from types import SimpleNamespace
    from mmw.core.publisher.manifest import now_stamp
    from mmw.core.repository_points import current_repository_points
    now = now or now_stamp()
    names = {n.name for n in networks}
    # Every held point any network saw, and every repository point.
    candidates = set(pool.linked_held_points())
    owned, by_repo_track = {}, {}
    for p in pool.repository_points():
        owned[p.id] = pool.track_of_point(p.id).repo
    candidates.update(owned)
    for pid, p in pool.points_of(sorted(candidates)).items():
        if not p.held:
            continue
        track = pool.track_of_point(pid)
        if p.scoped and not pool.point_scopes(pid) & names and not track.repo.startswith("network:"):
            continue                              # only removed networks had it
        by_repo_track.setdefault((track.repo, p.track), []).append(p)
    # A network's own points are current while its latest recording names them.
    theirs = {pid: repo for (repo, _), ps in by_repo_track.items() for p in ps
              if (pid := p.id) not in owned}
    recorded_now = {pid for n in networks for ids in pool.points_at(n.name, "99999999T999999Z").values()
                    for pid in ids if pid in theirs}
    kept: set[int] = set()
    # A history network still needs points observed since its last transfer.
    for n in networks:
        kept |= unsent_points(pool, n, {**owned, **theirs})
    kept |= pool.published_point_ids()
    # Explicit old snapshots remain compatibility roots until migrated.
    kept |= {pid for n in networks for snap in pool.snapshots(n.name) if snap.complete
             for pid in pool.snapshot_point_ids(snap.id)}
    for (repo, _), ps in by_repo_track.items():
        ps.sort(key=lambda p: (p.date, p.stamp, p.id), reverse=True)
        if repo.startswith("network:"):
            if repo.split(":", 1)[1] not in names:
                continue
            rule = SimpleNamespace(keep_points=cfg.settings.keep_points, keep_suites={})
        else:
            source = next((s for s in cfg.sources if s.identity == repo), None)
            if source is None:
                continue
            own = getattr(source, "keep_points", None)
            rule = SimpleNamespace(keep_points=cfg.settings.keep_points if own is None else own,
                                   keep_suites=getattr(source, "keep_suites", {}))
        keep = keep_for(rule, pool.track_of_point(ps[0].id))
        cutoff = _cutoff(now, int(keep[:-1])) if isinstance(keep, str) and keep != "all" else None
        current = set(current_repository_points(pool, repo)) | (recorded_now & {p.id for p in ps})
        ends = superseded_at(ps, now)
        for i, p in enumerate(ps):
            if (i == 0 or p.id in current or keep == "all" or (isinstance(keep, int) and i < keep)
                    or (cutoff is not None and ends[p.id] >= cutoff)):
                kept.add(p.id)
    kept |= set(pool.point_labels())          # a named point is never pruned
    return sorted(candidates - kept)


def apply_retention(pool: Pool, networks: list[NetworkConfig], *, now: str | None = None, cfg: Config) -> int:
    """Stub every point `retention_plan` names; how many."""
    gone = retention_plan(pool, networks, now=now, cfg=cfg)
    for pid in gone:
        pool.stub_point(pid)
    return len(gone)
