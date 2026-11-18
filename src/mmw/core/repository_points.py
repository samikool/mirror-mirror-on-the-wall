"""Repository-owned points and their network selections.

Unit identity is repository plus canonical dimensions, independent of names.
Each unit retains its own canonical file variants, including Release metadata.
Unit zero denotes shared upstream files.
"""
from __future__ import annotations

import json


def _project(rows):
    """Canonical conflicts resolve before by-hash, exactly as publish_group."""
    from mmw.core.publisher.links import _wants_by_hash
    chosen, shared = {}, {}
    for path, checksum, kind, hashed, common in rows:
        if common:
            shared[path] = checksum
        elif path not in chosen or checksum > chosen[path][0]:
            chosen[path] = (checksum, kind, hashed)
    out = {path: value[0] for path, value in chosen.items()}
    for path, (checksum, kind, hashed) in chosen.items():
        if hashed and _wants_by_hash(path, kind):
            parent = path.rsplit('/', 1)[0]
            out[f'{parent}/by-hash/SHA256/{checksum}'] = checksum
    out.update(shared)
    return out


def slice_files(pool, point_id: int, units: list[tuple[str, dict[str, str]]]) -> dict[str, str]:
    """Project immutable per-unit variants using the walk's conflict rules."""
    point = pool.points_of([point_id]).get(point_id)
    if point is None or not point.held:
        return {}
    if point.legacy:
        return pool.point_files(point_id)
    wanted = {(repo, json.dumps(dims, sort_keys=True, separators=(",", ":"))) for repo, dims in units}
    with pool._read() as c:
        variants = c.execute(
            "SELECT p.path, b.checksum, f.kind, f.hashed, f.unit, u.repo, u.dims "
            "FROM point_unit_files f JOIN snapshot_paths p ON p.id = f.path "
            "JOIN snapshot_blobs b ON b.id = f.blob LEFT JOIN units u ON u.id = f.unit "
            "WHERE f.point = ?", (point_id,)).fetchall()
        if not variants:  # Low-level membership callers, and earlier development pools.
            rows = c.execute(
                "SELECT p.path, b.checksum, u.repo, u.dims, m.unit FROM point_units m "
                "JOIN point_files f ON f.point = m.point AND f.path = m.path "
                "JOIN snapshot_paths p ON p.id = f.path JOIN snapshot_blobs b ON b.id = f.blob "
                "LEFT JOIN units u ON u.id = m.unit WHERE m.point = ?", (point_id,)).fetchall()
            return {path: checksum for path, checksum, repo, dims, unit in rows
                    if unit == 0 or (repo, dims) in wanted}
    return _project((path, checksum, kind, hashed, unit == 0)
                    for path, checksum, kind, hashed, unit, repo, dims in variants
                    if unit == 0 or (repo, dims) in wanted)


def record_repository(pool, repo, units, mirror_dir, stamp, *, refs=None, complete_suites=None):
    """Freeze an accumulated suite state, preserving each unit's own metadata."""
    from dataclasses import replace
    from mmw.core.points import split_repo
    from mmw.core.publisher.links import publish_group, publish_upstream_files, upstream_files, ROOT_RELATIVE_PREFIX, _dedup_rows
    from mmw.core.snapshots import RecordingSink
    from mmw.providers import get_provider
    from mmw.site.tracks import content_ident

    if complete_suites is not None:
        units = [u for u in units if u.suite in complete_suites]
    view = refs or pool
    groups = {u.mirror_base for u in units}
    settings = json.dumps({'repos': {repo: sorted(groups, key=len, reverse=True)}})
    members = []
    coordinates = {}
    for unit in units:
        coordinates.setdefault((unit.source, tuple(sorted(unit.dims.items()))), []).append(unit)
    for aliases in coordinates.values():
        unit = aliases[0]
        names = [u.name for u in aliases]
        one = RecordingSink(pool)
        result = publish_group(aliases, one, view, prune=False)
        if result.errors:
            raise ValueError(f"{unit.name}: " + "; ".join(result.errors[:5]))
        hashed = get_provider(unit.provider).by_hash
        raw = {f'{unit.mirror_base}/{path}': (cs, kind, hashed)
               for path, _, _, kind, cs in _dedup_rows(view.mirror_files(names))
               if not path.startswith(ROOT_RELATIVE_PREFIX)}
        drafts = {d.track: d for d in split_repo(pool, repo, one.rows, settings, stamp)}
        members.append((unit, raw, drafts))
    extra_sink = RecordingSink(pool)
    if refs is None or getattr(refs, "live", False):
        extra = {p: f for p, f in upstream_files(mirror_dir, units).items()
                 if any(p.startswith(b + '/') for b in groups)}
        if errors := publish_upstream_files(extra_sink, extra):
            raise ValueError('; '.join(errors))
    shared = {p: (cs, 'meta', False) for p, cs in extra_sink.rows.items()}
    full = _project((p, cs, k, h, False) for _, raw, _ in members for p, (cs, k, h) in raw.items())
    full.update(extra_sink.rows)
    recorded = []
    for draft in split_repo(pool, repo, full, settings, stamp):
        memberships = []
        dates = []
        for unit, raw, drafts in members:
            own = drafts.get(draft.track)
            paths = set(own.files) if own is not None else set()
            paths.update(draft.files)
            mine = {p: row for p, row in raw.items() if p in paths}
            if mine:
                memberships.append((unit.dims, mine))
                if own is not None:
                    dates.append(own.date)
        common = {p: row for p, row in shared.items() if p in draft.files}
        if common:
            memberships.append((None, common))
        projection = _project((p, cs, k, h, dims is None) for dims, raw in memberships
                              for p, (cs, k, h) in raw.items())
        identity = content_ident({json.dumps([dims, p, k, bool(h)], sort_keys=True): cs
                                  for dims, raw in memberships for p, (cs, k, h) in raw.items()})
        state = replace(draft, ident='state:' + identity, files=projection, date=max(dates, default=draft.date))
        pid, new = pool.record_point(repo, state, stamp)
        pool.record_unit_files(pid, repo, memberships)
        recorded.append((pid, new, state))
    return recorded


def wire_draft(pool, point_id, files, settings, stamp):
    """Upstream-shaped slice metadata; repository state identity stays local."""
    from mmw.core.points import split_repo
    from mmw.site.tracks import PointDraft
    p = pool.points_of([point_id])[point_id]
    track = pool.track_of_point(point_id)
    if p.legacy:
        return PointDraft(track.name, p.ident, p.date,
                          {k: v for k, v in [('Suite', track.suite), ('Codename', track.codename)] if v}, files)
    if not track.name:
        from mmw.site.tracks import content_ident
        return PointDraft('', content_ident(files), p.date, {}, files)
    # Metadata needs only this track's Release; never re-read package indexes
    # merely to project or list a retained state.
    release_rows = {path: files[path] for name in ('Release', 'InRelease', 'Release.gpg')
                    if (path := track.name + '/' + name) in files}
    drafts = split_repo(pool, track.repo, release_rows, settings, p.stamp)
    from dataclasses import replace
    return replace(next(d for d in drafts if d.track == track.name), files=files)


def current_repository_points(pool, repo):
    with pool._read() as c:
        row = c.execute('SELECT points FROM repository_current WHERE repo = ?', (repo,)).fetchone()
    return json.loads(row[0]) if row else []


def set_current_repository_points(pool, repo, recorded, *, replace=True):
    ids = [pid for pid, _, _ in recorded]
    if not replace:
        tracks = {p.track for p in pool.points_of(ids).values()}
        ids += [p.id for p in pool.points_of(current_repository_points(pool, repo)).values() if p.track not in tracks]
    with pool._lock:
        pool._conn.execute('INSERT INTO repository_current VALUES (?, ?) '
                           'ON CONFLICT(repo) DO UPDATE SET points = excluded.points', (repo, json.dumps(ids)))
        pool._conn.commit()


def selected_point_files(pool, pid, units):
    """A point outside the selected suites contributes no track-wide files."""
    track = pool.track_of_point(pid)
    mine = [u for u in units if u.source == track.repo]
    if not mine:
        return {}
    if track.name and not any(not u.suite or track.name == u.mirror_base + '/dists/' + u.suite for u in mine):
        # Flat repositories' Release sits at their base itself.
        if not any(track.name == u.mirror_base and not u.suite for u in mine):
            return {}
    return slice_files(pool, pid, [(u.source, u.dims) for u in mine])


def files_for_settings(pool, point_id, settings):
    """Use the selection saved with a recording, never today's config."""
    point = pool.points_of([point_id]).get(point_id)
    if point is None:
        return {}
    if point.legacy:
        return pool.point_files(point_id)
    from mmw.core.config import MirrorConfig
    units = [MirrorConfig.model_validate(u) for u in json.loads(settings or '{}').get('mirrors', [])]
    return selected_point_files(pool, point_id, units)


def current_ref_projection(pool, units, shared):
    """Check committed refs without walking files or parsing package indexes."""
    from mmw.core.publisher.links import ROOT_RELATIVE_PREFIX
    from mmw.providers import get_provider
    groups = {}
    for unit in units:
        groups.setdefault(unit.mirror_base, []).append(unit)
    rows = []
    for base, members in groups.items():
        hashed = any(get_provider(u.provider).by_hash for u in members)
        rows.extend((f'{base}/{path}', cs, kind, hashed, False)
                    for path, _, _, kind, cs in pool.mirror_files([u.name for u in members])
                    if not path.startswith(ROOT_RELATIVE_PREFIX))
    projected = _project(rows)
    projected.update(shared)
    return projected


def shared_files(pool, point_ids):
    with pool._read() as c:
        out = {}
        for pid in point_ids:
            out.update(c.execute('SELECT p.path, b.checksum FROM point_unit_files f '
                                 'JOIN snapshot_paths p ON p.id = f.path JOIN snapshot_blobs b ON b.id = f.blob '
                                 'WHERE f.point = ? AND f.unit = 0', (pid,)).fetchall())
    return out


def cache_slice(pool, network, point_id, files, units):
    from mmw.site.tracks import content_ident
    track = pool.track_of_point(point_id)
    settings = json.dumps({'repos': {track.repo: sorted({u.mirror_base for u in units if u.source == track.repo})}})
    wire = wire_draft(pool, point_id, files, settings, pool.points_of([point_id])[point_id].stamp)
    pool.set_network_slice(network, point_id, content_ident(files), date=wire.date)


class _AccumulatedRefs:
    live = True

    def __init__(self, pool, previous):
        self.pool, self.previous = pool, previous

    def mirror_files(self, mirror_names, kinds=None, prefix=''):
        rows = []
        for name in mirror_names:
            if name in self.previous:
                rows.extend(row for row in self.previous[name]
                            if (not kinds or row[3] in kinds) and row[0].startswith(prefix))
            else:
                rows.extend(self.pool.mirror_files([name], kinds=kinds, prefix=prefix))
        return sorted(set(rows))

    def __getattr__(self, name):
        return getattr(self.pool, name)


def accumulated_refs(pool, repo, units, successes):
    """Unattempted units retain recorded good refs, never failed live refs."""
    if successes is None:
        return pool, [u for u in units if pool.has_refs(u.name)]
    previous_ids = current_repository_points(pool, repo)
    failed = pool.failed_units(repo)
    previous, kept = {}, []
    for unit in units:
        raw = json.dumps(unit.dims, sort_keys=True, separators=(',', ':'))
        if (unit.name in successes or raw not in failed) and pool.has_refs(unit.name):
            kept.append(unit)
            continue
        records = []
        with pool._read() as c:
            for pid in previous_ids:
                records.extend(c.execute(
                    'SELECT p.path, b.checksum, f.kind FROM point_unit_files f '
                    'JOIN snapshot_paths p ON p.id = f.path JOIN snapshot_blobs b ON b.id = f.blob '
                    'JOIN units u ON u.id = f.unit WHERE f.point = ? AND u.repo = ? AND u.dims = ?',
                    (pid, repo, raw)).fetchall())
        if records:
            info = pool.blob_info(cs for _, cs, _ in records)
            if any(cs not in info for _, cs, _ in records):
                raise ValueError(f'{unit.name}: last recorded files are missing — verify or sync this unit again')
            prefix = unit.mirror_base + '/'
            previous[unit.name] = [(path[len(prefix):], str(info[cs][0]), info[cs][1], kind, cs)
                                   for path, cs, kind in records if path.startswith(prefix)]
            kept.append(unit)
    return _AccumulatedRefs(pool, previous), kept
