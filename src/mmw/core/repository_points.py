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


def provider_scoped(repo: str) -> bool:
    """One repository for every source of its provider (PyPI, VS Code): its
    history is one `""` track, and a network's slice of a point is the
    provider's own publish over it (`network_slice`)."""
    from mmw.providers import get_provider
    try:
        return get_provider(repo).repository_scope == "provider"
    except ValueError:
        return False


def _raw(coords: dict) -> str:
    return json.dumps(coords, sort_keys=True, separators=(",", ":"))


def _point_day(stamp: str):
    from datetime import date
    return date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))


class PointRefs:
    """Points of a provider-scoped repository read as a pool's refs.

    `mirror_files` / `resolve` / `ref_map` / `meta_refs` / `has_refs` answer
    per unit name from the points' unit rows, by `coordinates(unit)`;
    everything else is the pool's. A provider's `publish` / `contribute` run
    over it lay out exactly what synced then (`as_of`: the points' day)."""
    live = False

    def __init__(self, pool, repo, point_ids, units, *, coords=None, as_of=None,
                 retention=None, name=None):
        self.pool, self.repo = pool, repo
        self.point_ids = sorted(set(point_ids))
        self.units = list(units)
        self._coords = coords if coords is not None else {u.name: coordinates(u) for u in self.units}
        if as_of is None and self.point_ids:
            as_of = max(_point_day(p.stamp) for p in pool.points_of(self.point_ids).values())
        self.as_of = as_of
        self.retention = retention          # the network's as recorded (None: unknown — a slice keeps all)
        self.name = name
        self._rows: dict[str, list] = {}

    def _unit_rows(self, name: str) -> list:
        if name not in self._rows:
            coords = self._coords.get(name)
            found = []
            if coords is not None and self.point_ids:
                marks = ",".join("?" * len(self.point_ids))
                with self.pool._read() as c:
                    found = c.execute(
                        "SELECT p.path, b.checksum, f.kind, k.filename, k.size FROM point_unit_files f "
                        "JOIN units u ON u.id = f.unit JOIN snapshot_paths p ON p.id = f.path "
                        "JOIN snapshot_blobs b ON b.id = f.blob JOIN packages k ON k.checksum = b.checksum "
                        f"WHERE f.point IN ({marks}) AND u.repo = ? AND u.dims = ?",
                        (*self.point_ids, self.repo, _raw(coords))).fetchall()
            blob = self.pool._blob_strs()
            self._rows[name] = sorted({(path, blob(cs, fn), size, kind, cs) for path, cs, kind, fn, size in found})
        return self._rows[name]

    def mirror_files(self, mirror_names, kinds=None, prefix=''):
        rows = {row for name in mirror_names for row in self._unit_rows(name)
                if (not kinds or row[3] in kinds) and row[0].startswith(prefix)}
        return sorted(rows)

    def resolve(self, mirror_name, repo_path):
        from pathlib import Path
        return next((Path(row[1]) for row in self._unit_rows(mirror_name) if row[0] == repo_path), None)

    def ref_map(self, mirror_name):
        return {row[0]: row[4] for row in self._unit_rows(mirror_name)}

    def meta_refs(self, mirror_name):
        from mmw.core.pool import KIND_META
        return {row[0]: row[4] for row in self._unit_rows(mirror_name) if row[3] == KIND_META}

    def has_refs(self, mirror_name):
        return bool(self._unit_rows(mirror_name))

    def layer_overlay(self, *_):
        return None

    def __getattr__(self, name):
        return getattr(self.pool, name)


def _recorded_selects(network: dict) -> list:
    from mmw.core.config import NetworkSelect
    selects = []
    for sel in network.get('select') or []:
        dims = sel.get('dims') if 'dims' in sel else {k: v for k, v in sel.items() if k != 'source'}
        # Read, not validated: a recorded select is dumped with the dims table
        # the config refuses (the TOML spells dimensions flat).
        selects.append(NetworkSelect.model_construct(source=sel.get('source', ''), dims=dims or {}))
    return selects


def provider_view(pool, repo, point_ids, *, settings=None, units=None, network=None):
    """A network's view of points of a provider-scoped repository, or None
    when it selects none of it. The selection comes from recorded `settings`
    (the network's select, its retention, the source it carried) or, live,
    from today's `units` and `network`. Its units are the points' own, by
    coordinate: recorded settings keep no dims, and a point is what synced."""
    from mmw.core.config import MirrorConfig
    from mmw.core.repos import repository_key
    ids = [pid for pid, p in pool.points_of(point_ids).items() if p.held and not p.legacy]
    if not ids:
        return None
    name = getattr(network, 'name', network)
    if settings is not None:
        data = json.loads(settings or '{}')
        recorded = data.get('network') or {}
        name = recorded.get('name') or name
        retention = recorded.get('retention')             # older settings: none, so forever
        sources = sorted({m.get('source') or '' for m in data.get('mirrors', []) if m.get('provider') == repo})
        if not sources:
            return None
        source, selects = sources[0], _recorded_selects(recorded)

        def wanted(unit, _coords):
            return not selects or any(sel.matches(unit) for sel in selects)
    else:
        retention = getattr(network, 'retention', None)
        mine = [u for u in units or [] if repository_key(u) == repo]
        if not mine:
            return None
        source = mine[0].source
        chosen = {_raw(coordinates(u)) for u in mine}

        def wanted(_unit, coords):
            return _raw(coords) in chosen
    marks = ",".join("?" * len(ids))
    with pool._read() as c:
        held = [row[0] for row in c.execute(
            f"SELECT DISTINCT u.dims FROM point_unit_files f JOIN units u ON u.id = f.unit "
            f"WHERE f.point IN ({marks}) AND u.repo = ? ORDER BY u.dims", (*ids, repo))]
    picked, coords = [], {}
    for raw in held:
        dims = json.loads(raw)
        unit = MirrorConfig.model_validate({'name': f'{repo}:{raw}', 'provider': repo, 'source_url': source,
                                            'source': source, 'dims': dims})
        if wanted(unit, dims):
            picked.append(unit)
            coords[unit.name] = dims
    return PointRefs(pool, repo, ids, picked, coords=coords, retention=retention, name=name)


class _SliceSink:
    """Records the repository's files a publish links; pages are the network's."""

    def __init__(self, pool):
        from mmw.core.snapshots import RecordingSink
        self._inner = RecordingSink(pool)
        self.rows, self.errors = self._inner.rows, self._inner.errors

    def write(self, rel, data):
        pass

    def __getattr__(self, name):
        return getattr(self._inner, name)


def slice_of(view) -> dict[str, str]:
    """The files `view`'s provider publishes under its own bases: the
    network's slice of those points. Cached per pool: points don't change."""
    if view is None or not view.units:
        return {}
    cache = view.pool.__dict__.setdefault('_provider_slices', {})
    key = (view.repo, tuple(view.point_ids), tuple(sorted(_raw(c) for c in view._coords.values())),
           view.retention, view.as_of)
    if key not in cache:
        from types import SimpleNamespace
        from mmw.core.publisher.results import NetworkPublishResult
        from mmw.providers import get_provider
        from mmw.providers.base import PublishContext
        sink = _SliceSink(view.pool)
        ctx = PublishContext(network=SimpleNamespace(name=view.name or '', retention=view.retention or 'forever'),
                             cfg=None, mirror_dir=None, pool=view, sink=sink,
                             result=NetworkPublishResult(network_name=view.name or ''), units=view.units,
                             as_of=view.as_of, files_only=True)
        _, bases = get_provider(view.repo).publish(ctx, view.units)
        if sink.errors:
            raise ValueError(f'{view.repo}: ' + '; '.join(sink.errors[:5]))
        cache[key] = {p: cs for p, cs in sink.rows.items() if any(p.startswith(b + '/') for b in bases)}
    return dict(cache[key])


def point_pages(pool, point_id: int, settings, network, cfg, mirror_dir, sink) -> dict[str, str]:
    """{path: checksum} of the pages a provider-scoped point brings for
    `network` (a NetworkConfig): PyPI's shards and renderer, the VS Code
    gallery's catalog, updates and notes — generated from the point through
    the same view and context a publish over it uses, written through `sink`
    (into the pool, or only hashed for a preview). They travel with the
    point, so a far side serving it serves its pages. {} for any other point,
    or one no view of this network serves (a converted point whose pages are
    still the network's)."""
    point = pool.points_of([point_id]).get(point_id)
    track = pool.track_of_point(point_id)
    if point is None or not point.held or track is None or not provider_scoped(track.repo):
        return {}
    repo = track.repo
    view = provider_view(pool, repo, [point_id], settings=settings, network=network.name)
    if view is None:
        from mmw.core.transfer_ranges import _equal_view
        view = _equal_view(pool, repo, network_slice(pool, network.name, point_id, settings), settings,
                           network.name)
    if view is None or not view.units:
        return {}
    from mmw.core.publisher.results import NetworkPublishResult
    from mmw.providers import get_provider
    from mmw.providers.base import PublishContext
    recorded = network
    if view.retention not in (None, network.retention):            # that day's, as its slice
        recorded = network.model_copy(update={"retention": view.retention})
    ctx = PublishContext(network=recorded, cfg=cfg, mirror_dir=mirror_dir, pool=view, sink=sink,
                         result=NetworkPublishResult(network_name=network.name), units=list(view.units),
                         as_of=view.as_of)
    out = {}
    for rel, data in sorted(get_provider(repo).pages(ctx, view.units).items()):
        sink.write(rel, data)
        out[rel] = sink.rows[rel]
    return out


def network_slice(pool, network, point_id: int, settings, *, when=None) -> dict[str, str]:
    """What `network` (a name, a NetworkConfig, or None) serves of a point,
    under the selection recorded in `settings`. A provider-scoped point:
    its provider's publish over the point, as of the point's day (D4); a
    point of a repository the recorded network layers: its newest layer view
    stamped at or before `when` (None: now), else the plain slice; a scoped
    point: its files, to its own networks. Every reader gets this one slice."""
    point = pool.points_of([point_id]).get(point_id)
    if point is None or not point.held:
        return {}
    data = json.loads(settings or '{}')
    recorded = data.get('network') or {}
    name = getattr(network, 'name', network) or recorded.get('name') or None
    if point.legacy:
        return scoped_files(pool, point_id, name)
    repo = pool.track_of_point(point_id).repo
    if provider_scoped(repo):
        return slice_of(provider_view(pool, repo, [point_id], settings=settings, network=network))
    from mmw.core.config import MirrorConfig
    units = [MirrorConfig.model_validate(u) for u in data.get('mirrors', [])]
    plain = selected_point_files(pool, point_id, units, name)
    if plain and name and _layered(recorded, repo):
        from mmw.core.layers import view_for
        view = view_for(pool, name, point_id, when, plain=plain)
        if view is not None:
            return view
    return plain


def _layered(recorded_network: dict, repo: str) -> bool:
    """Whether a recorded network (its settings' `network`) layers `repo`."""
    from mmw.core.config import normalize_url
    return any(normalize_url(layer.get('source') or '') == repo
               for layer in recorded_network.get('layers') or [] if isinstance(layer, dict))


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

    if provider_scoped(repo):
        return _record_provider_repository(pool, repo, units, stamp, refs=refs)
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


def _record_provider_repository(pool, repo, units, stamp, *, refs=None):
    """One `""` point of a provider-scoped repository: per coordinate, the
    unit's refs, its provider's private files (`.pypi-meta/`, …) included,
    so a slice can be published from the point alone. The point's files are
    what it can serve: no private path. Its bases are the hosts its
    provider's publish lays them under, not `mirror_base`."""
    from dataclasses import replace
    from types import SimpleNamespace
    from mmw.core.points import split_repo
    from mmw.core.publisher.links import _dedup_rows
    from mmw.core.publisher.results import NetworkPublishResult
    from mmw.providers import get_provider
    from mmw.providers.base import PublishContext
    from mmw.site.tracks import content_ident

    provider = get_provider(repo)
    view = refs or pool
    by_coordinate = {}
    for unit in units:
        by_coordinate.setdefault(_raw(coordinates(unit)), []).append(unit)
    memberships = []
    for aliases in by_coordinate.values():
        raw = {path: (cs, kind, False)
               for path, _, _, kind, cs in _dedup_rows(view.mirror_files([u.name for u in aliases]))}
        if raw:
            memberships.append((coordinates(aliases[0]), raw))
    if not memberships:
        return []
    private = provider.private_prefixes
    files = _project((p, cs, k, h, False) for _, raw in memberships for p, (cs, k, h) in raw.items()
                     if not (private and p.startswith(private)))
    sink = _SliceSink(pool)
    ctx = PublishContext(network=SimpleNamespace(name='', retention='forever'), cfg=None, mirror_dir=None,
                         pool=view, sink=sink, result=NetworkPublishResult(network_name=''),
                         units=list(units), as_of=_point_day(stamp), files_only=True)
    _, bases = provider.publish(ctx, list(units))
    if sink.errors:
        raise ValueError(f'{repo}: ' + '; '.join(sink.errors[:5]))
    settings = json.dumps({'repos': {repo: sorted(bases, key=len, reverse=True)}})
    drafts = split_repo(pool, repo, files, settings, stamp)
    if [d.track for d in drafts] != ['']:
        raise ValueError(f'{repo}: expected one content track, found {sorted(d.track for d in drafts)}')
    identity = content_ident({json.dumps([dims, p, k, bool(h)], sort_keys=True): cs
                              for dims, raw in memberships for p, (cs, k, h) in raw.items()})
    state = replace(drafts[0], ident='state:' + identity, files=files)
    pid, new = pool.record_point(repo, state, stamp)
    pool.record_unit_files(pid, repo, memberships)
    return [(pid, new, state)]


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


def selected_point_files(pool, pid, units, network=None, *, when=None):
    """A point outside the selected suites contributes no track-wide files.
    `network` (a name or a NetworkConfig): whose slice — a scoped point is its
    networks' only; a provider-scoped point is narrowed by its retention; a
    NetworkConfig that layers the repository gets its newest view stamped at
    or before `when` (a name: the plain slice, what a view is built over)."""
    track = pool.track_of_point(pid)
    point = pool.points_of([pid]).get(pid)
    if point is not None and not point.legacy and provider_scoped(track.repo):
        return slice_of(provider_view(pool, track.repo, [pid], units=units, network=network))
    if point is not None and not point.legacy and hasattr(network, 'layer_for') \
            and network.layer_for(track.repo) is not None:
        plain = selected_point_files(pool, pid, units, network.name)
        from mmw.core.layers import view_for
        view = view_for(pool, network.name, pid, when, plain=plain) if plain else None
        return view if view is not None else plain
    network = getattr(network, 'name', network)
    mine = [u for u in units if u.source == track.repo]
    if not mine:
        return {}
    if track.name and not any(not u.suite or track.name == u.mirror_base + '/dists/' + u.suite for u in mine):
        # Flat repositories' Release sits at their base itself.
        if not any(track.name == u.mirror_base and not u.suite for u in mine):
            return {}
    return slice_files(pool, pid, [(u.source, coordinates(u)) for u in mine], network=network)


def files_for_settings(pool, point_id, settings, *, when=None):
    """Use the selection saved with a recording, never today's config:
    `network_slice` for the network it names (`when`: the moment read)."""
    return network_slice(pool, None, point_id, settings, when=when)


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
            prefix = '' if provider_scoped(repo) else unit.mirror_base + '/'
            previous[unit.name] = [(path[len(prefix):], str(info[cs][0]), info[cs][1], kind, cs)
                                   for path, cs, kind in records if path.startswith(prefix)]
            kept.append(unit)
    return _AccumulatedRefs(pool, previous), kept
