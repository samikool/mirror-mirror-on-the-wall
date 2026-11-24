"""Repository-owned points and their network selections.

Unit identity is repository plus canonical dimensions, independent of names.
Each unit retains its own canonical file variants, including Release metadata.
Unit zero denotes shared upstream files.
"""
from __future__ import annotations

import json


def _project(rows, read=None, groups=()):
    """Resolve canonical files before by-hash, choosing trust roots by Date."""
    from mmw.core.publisher.links import _wants_by_hash
    from mmw.core.point_check import newest_release
    rows = list(rows)
    chosen, shared = {}, {}
    variants = {}
    for path, checksum, *_ in rows:
        if path.endswith(("/Release", "/InRelease")):
            variants.setdefault(path, []).append(checksum)
    roots, omitted = {}, set()
    if read:
        from mmw.core.apt_index import release_body
        suites = {}
        for path, checksums in variants.items():
            suites.setdefault(path.rsplit('/', 1)[0], {})[path] = checksums
        for directory, suite_variants in suites.items():
            winner = newest_release(suite_variants, read)
            body = release_body(read(winner) or b'')
            for path, checksums in suite_variants.items():
                matching = [cs for cs in checksums if release_body(read(cs) or b'') == body]
                if matching:
                    roots[path] = max(matching)
                else:
                    omitted.add(path)
    # A detached signature must follow its own Release, never checksum order.
    signatures = {}
    for group in groups:
        for path, row in group.items():
            if path.endswith('/Release') and roots.get(path) == row[0]:
                sig = path + '.gpg'
                if sig in group:
                    signatures[sig] = max(signatures.get(sig, ""), group[sig][0])
    if read:
        omitted.update(path for path, *_ in rows if path.endswith('/Release.gpg')
                       and path not in signatures)
    for path, checksum, kind, hashed, common in rows:
        if path in omitted:
            continue
        if common:
            shared[path] = checksum
        elif (path not in roots or roots[path] == checksum) and (path not in signatures or signatures[path] == checksum):
            if path not in chosen or checksum > chosen[path][0]:
                chosen[path] = (checksum, kind, hashed)
    out = {path: value[0] for path, value in chosen.items()}
    for path, (checksum, kind, hashed) in chosen.items():
        if hashed and _wants_by_hash(path, kind):
            parent = path.rsplit('/', 1)[0]
            out[f'{parent}/by-hash/SHA256/{checksum}'] = checksum
    out.update(shared)
    return out


def _blob_reader(pool):
    from functools import lru_cache
    from mmw.core import disk_io
    @lru_cache(maxsize=64)
    def read(checksum):
        path = pool.blob_file(checksum)
        try:
            return disk_io.ops.read_bytes(path) if path is not None else None
        except OSError:
            return None
    return read


def coordinates(unit) -> dict:
    """A unit's identity within its repository (`units.dims`): its provider's
    `unit_coordinates` — its dims, plus whatever else tells two units apart."""
    from mmw.providers import get_provider
    return get_provider(unit.provider).unit_coordinates(unit)


def scoped_files(pool, point_id: int, network: str | None) -> dict[str, str]:
    """A scoped point's files: all of them to a network it is scoped to (or
    to no network in particular), nothing to any other. One with no scope
    (unconverted, only ever read inside the upgrade chain) serves as it did."""
    scopes = pool.point_scopes(point_id)
    if network is None or not scopes or network in scopes:
        return pool.point_files(point_id)
    return {}


def slice_files(pool, point_id: int, units: list[tuple[str, dict[str, str]]], *,
                network: str | None = None) -> dict[str, str]:
    """Project immutable per-unit variants using the walk's conflict rules."""
    point = pool.points_of([point_id]).get(point_id)
    if point is None or not point.held:
        return {}
    if point.legacy:
        return scoped_files(pool, point_id, network)
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
    selected = [row for row in variants if row[4] == 0 or (row[5], row[6]) in wanted]
    groups = {}
    for path, checksum, kind, hashed, unit, *_ in selected:
        groups.setdefault(unit, {})[path] = (checksum, kind, hashed)
    return _project(((path, checksum, kind, hashed, unit == 0)
                     for path, checksum, kind, hashed, unit, *_ in selected),
                    _blob_reader(pool), groups.values())


def record_repository(pool, repo, units, mirror_dir, stamp, *, refs=None, complete_suites=None, say=None):
    """Freeze an accumulated suite state, preserving each unit's own metadata."""
    from dataclasses import replace
    from mmw.core.points import split_repo, _reader
    from mmw.core.point_check import check_suite
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
    by_coordinate = {}
    for unit in units:
        by_coordinate.setdefault((unit.source, json.dumps(coordinates(unit), sort_keys=True)), []).append(unit)
    for aliases in by_coordinate.values():
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
        unreadable = sorted({path for draft in drafts.values() for path in draft.unreadable})
        if unreadable:
            raise ValueError(f"{unit.name}: cannot record a complete repository point: "
                             f"unreadable required indexes or Release metadata ({', '.join(unreadable[:3])}); "
                             "check the configured pool with `mmw verify --stat` and restore missing files")
        members.append((unit, raw, drafts))
    extra_sink = RecordingSink(pool)
    if refs is None or getattr(refs, "live", False):
        extra = {p: f for p, f in upstream_files(mirror_dir, units).items()
                 if any(p.startswith(b + '/') for b in groups)}
        if errors := publish_upstream_files(extra_sink, extra):
            raise ValueError('; '.join(errors))
    shared = {p: (cs, 'meta', False) for p, cs in extra_sink.rows.items()}
    read_blob = _blob_reader(pool)
    full = _project(((p, cs, k, h, False) for _, raw, _ in members for p, (cs, k, h) in raw.items()),
                    read_blob, [raw for _, raw, _ in members])
    full.update(extra_sink.rows)
    recorded = []
    for draft in split_repo(pool, repo, full, settings, stamp):
        memberships = []
        for unit, raw, drafts in members:
            own = drafts.get(draft.track)
            paths = set(own.files) if own is not None else set()
            paths.update(draft.files)
            mine = {p: row for p, row in raw.items() if p in paths}
            if mine:
                memberships.append((coordinates(unit), mine))
        common = {p: row for p, row in shared.items() if p in draft.files}
        if common:
            memberships.append((None, common))
        projection = _project(((p, cs, k, h, dims is None) for dims, raw in memberships
                               for p, (cs, k, h) in raw.items()), read_blob, [raw for _, raw in memberships])
        problems = check_suite(projection, draft.track, _reader(pool, projection))
        if problems:
            if say is not None:
                warned = set()
                for unit, raw, drafts in members:
                    own = drafts.get(draft.track)
                    if own is None:
                        continue
                    stale = any(p.endswith(('/Release', '/InRelease')) and p in projection
                                and projection[p] != row[0] for p, row in raw.items())
                    label = unit.component or unit.name
                    if stale and label not in warned:
                        say(f'  ⚠ {label} synced against an older Release — sync it again')
                        warned.add(label)
                say(f'  ⚠ {draft.fields.get("Suite") or draft.track}: point not recorded — ' + '; '.join(problems[:5]))
            continue
        identity = content_ident({json.dumps([dims, p, k, bool(h)], sort_keys=True): cs
                                  for dims, raw in memberships for p, (cs, k, h) in raw.items()})
        state = replace(draft, ident='state:' + identity, files=projection)
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


def selected_point_files(pool, pid, units, network=None):
    """A point outside the selected suites contributes no track-wide files.
    `network`: whose slice — a scoped point is its networks' only."""
    track = pool.track_of_point(pid)
    mine = [u for u in units if u.source == track.repo]
    if not mine:
        return {}
    if track.name and not any(not u.suite or track.name == u.mirror_base + '/dists/' + u.suite for u in mine):
        # Flat repositories' Release sits at their base itself.
        if not any(track.name == u.mirror_base and not u.suite for u in mine):
            return {}
    return slice_files(pool, pid, [(u.source, coordinates(u)) for u in mine], network=network)


def files_for_settings(pool, point_id, settings):
    """Use the selection saved with a recording, never today's config."""
    point = pool.points_of([point_id]).get(point_id)
    if point is None:
        return {}
    data = json.loads(settings or '{}')
    network = (data.get('network') or {}).get('name') or None    # older settings name none
    if point.legacy:
        return scoped_files(pool, point_id, network)
    from mmw.core.config import MirrorConfig
    units = [MirrorConfig.model_validate(u) for u in data.get('mirrors', [])]
    return selected_point_files(pool, point_id, units, network)


def current_ref_projection(pool, units, shared):
    """Check committed refs without walking files or parsing package indexes."""
    from mmw.core.publisher.links import ROOT_RELATIVE_PREFIX
    from mmw.providers import get_provider
    groups = {}
    for unit in units:
        groups.setdefault(unit.mirror_base, []).append(unit)
    rows = []
    release_groups = []
    for base, members in groups.items():
        hashed = any(get_provider(u.provider).by_hash for u in members)
        for unit in members:
            raw = {f'{base}/{path}': (cs, kind, hashed)
                   for path, _, _, kind, cs in pool.mirror_files([unit.name])
                   if not path.startswith(ROOT_RELATIVE_PREFIX)}
            release_groups.append(raw)
            rows.extend((path, cs, kind, h, False) for path, (cs, kind, h) in raw.items())
    projected = _project(rows, _blob_reader(pool), release_groups)
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
        raw = json.dumps(coordinates(unit), sort_keys=True, separators=(',', ':'))
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
