"""Read-only point selection shared by transfer preview and bundle writing."""
from dataclasses import dataclass

from mmw.core.repository_points import files_for_settings, wire_draft
from mmw.core.snapshots import RecordingSink


class PreviewSink(RecordingSink):
    """The shared tree generator, with no pool or filesystem mutations."""

    def __init__(self, pool):
        super().__init__(pool)
        self.info = {}

    def link(self, src, rel, size, checksum, kind):
        self.rows[rel] = checksum
        self.info[checksum] = (src, size)
        return 'linked'

    link_pooled = link

    def write(self, rel, data):
        import hashlib
        if rel.startswith('nginx/'):
            return
        checksum = hashlib.sha256(data).hexdigest()
        self.rows[rel] = checksum
        self.info[checksum] = (None, len(data))


def repository_owned(pool, current, network=None):
    ids = [pid for repo, ids in current.items()
           if not repo.startswith('network:') and (network is None or network.layer_for(repo) is None)
           for pid in ids]
    return bool(ids) and all(not p.legacy for p in pool.points_of(ids).values())


def repository_projection(pool, network, cfg, mirror_dir, *, point_ids=None):
    """Current configured slices; legacy providers keep their existing walk."""
    from mmw.core.repository_points import selected_point_files
    from mmw.core.resolve import network_mirrors
    from mmw.core.repos import repository_key
    units = network_mirrors(network, cfg, mirror_dir)
    projection = {}
    for repo in {repository_key(u) for u in units}:
        if network.layer_for(repo) is not None:
            continue
        ids = repository_targets(pool, repo)
        if not ids:
            continue
        slices = {pid: selected_point_files(pool, pid, units) for pid in ids}
        files = {p: cs for mine in slices.values() for p, cs in mine.items()}
        if not files:
            raise ValueError(f'{repo}: selected repository point is no longer kept — verify or choose another point')
        projection[repo] = files
        if point_ids is not None:
            point_ids.update(pid for pid, mine in slices.items() if mine)
    return projection


def selected_tree(pool, network, cfg, mirror_dir, settings, current, *, until=None,
                  preview=False, progress=None, stop_event=None, point_ids=None):
    """Generate a network view from immutable points, never from live refs."""
    from mmw.core.snapshots import walk_tree, recorded_repos
    from mmw.core.repos import attribute
    targets, _, _ = select_range(pool, network.name, settings, current, until=until)
    if point_ids is not None:
        point_ids.update(pid for ids in targets.values() for pid in ids)
    projection = {repo: {path: cs for pid in ids for path, cs in files_for_settings(pool, pid, settings).items()}
                  for repo, ids in targets.items() if not repo.startswith('network:')}
    from mmw.core.resolve import network_mirrors
    from mmw.core.repos import repository_key
    for unit in network_mirrors(network, cfg, mirror_dir):
        projection.setdefault(repository_key(unit), {})
    sink = PreviewSink(pool) if preview else RecordingSink(pool)
    rows, settings, warnings = walk_tree(
        network, cfg, mirror_dir, pool, repository_files=projection, sink=sink,
        preview=preview, build_layers=False, progress=progress, stop_event=stop_event)
    repos = recorded_repos(settings)
    extras = {p: c for p, c in rows.items() if attribute(p, repos) is None}
    return rows, settings, extras, warnings, sink.info if preview else {}


def repository_targets(pool, repo):
    from mmw.core.repository_points import current_repository_points
    ids = current_repository_points(pool, repo)
    if ids:
        return ids
    newest = {}
    for point in pool.repository_points(repo, stubs=True):
        old = newest.get(point.track)
        if old is None or (point.date, point.stamp, point.ident) > (old.date, old.stamp, old.ident):
            newest[point.track] = point
    return [p.id for p in newest.values()]


def recorded_selection(pool, network, mirror_dir, cfg=None, *, only=None):
    """Read retained repository states even before a network has a recording.

    Sync owns the states. A new network only selects their unit slices;
    previewing it must neither resync nor manufacture historical states.
    """
    import json

    from mmw.core.points import current_points
    from mmw.core.resolve import network_mirrors
    from mmw.core.repos import repository_key

    settings = pool.settings_at(network.name, '99999999T999999Z')
    # Snapshot conversion seeds recordings without rebuilding the sync cache.
    # Use those recordings when no cached current state exists. If the cache
    # does exist it records the actual last walk, even when an imported or
    # explicitly dated recording has a later timestamp.
    current = pool.latest_recordings(network.name)
    current.update(current_points(pool, network.name))
    if cfg is None:
        if not settings:
            raise ValueError('No recorded points for this network — sync its repositories first.')
        return settings, current
    units = network_mirrors(network, cfg, mirror_dir)
    repos = {}
    for unit in units:
        repos.setdefault(repository_key(unit), []).append(unit)
    if only is not None:
        unknown = set(only) - set(repos)
        if unknown:
            raise ValueError(f'unknown repository {sorted(unknown)[0]!r}')
        repos = {repo: mine for repo, mine in repos.items() if repo in only}
    # A network's cached view must not hide states synced before it existed,
    # nor keep a changed selection tied to that old view.
    missing = {repo: mine for repo, mine in repos.items()
               if not current.get(repo) or (network.layer_for(repo) is None and repository_targets(pool, repo))}
    if not missing and settings:
        return settings, current
    data = json.loads(settings) if settings else {}
    data.update(nginx_container=2, network=network.model_dump(mode='json'),
                mirrors=[u.legacy_dict() for u in units])
    bases = data.setdefault('repos', {})
    for repo, mine in repos.items():
        if repo in missing or repo not in bases:
            bases[repo] = sorted({u.mirror_base for u in mine}, key=len, reverse=True)
    data['bases'] = sorted(set(data.get('bases', [])) | {base for group in bases.values() for base in group})
    from mmw.core.publisher.links import upstream_file_hosts, upstream_files
    from mmw.providers import extra_hosts
    data['upstream_hosts'] = upstream_file_hosts(upstream_files(mirror_dir, units))
    data['extra_hosts'] = extra_hosts(network, units)
    settings = json.dumps(data, sort_keys=True)
    for repo in missing:
        if network.layer_for(repo) is not None:
            raise ValueError(f'{repo}: no recorded state for this network\'s layer — sync it first.')
        ids = repository_targets(pool, repo)
        selected = [pid for pid in ids if files_for_settings(pool, pid, settings)]
        if not selected:
            raise ValueError(f'{repo}: no retained points match the selected suites and architectures '
                             '— sync this repository first.')
        current[repo] = selected
    current = {repo: ids for repo, ids in current.items() if repo in repos or repo.startswith('network:')}
    if repository_owned(pool, current, network):
        current = {repo: ids for repo, ids in current.items() if not repo.startswith('network:')}
    if not current:
        raise ValueError('No recorded points for this network — sync its repositories first.')
    return settings, current


def retained_track_points(pool, network, track, current, settings):
    """Retained repository states, sliced by this network's selection."""
    from mmw.site.tracks import content_ident
    points = {p.id: p for p in current.values() if p.track == track}
    for point in pool.network_points(network, track, stubs=True):
        points.setdefault(point.id, point)
    owned = next((p for p in points.values() if not p.legacy), None)
    if owned is not None:
        repo = pool.track_of_point(owned.id).repo
        for point in pool.repository_points(repo, stubs=True):
            if point.track == track:
                points.setdefault(point.id, point)
    selected, seen = [], set()
    for point in points.values():
        if not point.held:
            selected.append(point)
            continue
        files = files_for_settings(pool, point.id, settings)
        if not files:
            continue
        identity = content_ident(files)
        if identity not in seen:
            seen.add(identity)
            selected.append(point)
    return selected


def point_date(pool, point, settings):
    if point.legacy:
        return point.date
    if not point.held:
        return pool.network_slice_date(settings_network(settings), point.id) or point.date
    files = files_for_settings(pool, point.id, settings)
    return wire_draft(pool, point.id, files, settings, point.stamp).date if files else point.date


def transferred_slice(pool, network, point, settings):
    """Recover the selected slice named by the last-transfer convenience record.

    Its digest must match: stale settings or a missing ledger can only make
    us carry more, never omit data on the strength of an assumed selection.
    """
    import json
    from mmw.core import disk_io
    from mmw.core.publisher.manifest import ledger_dir, MANIFEST_SUFFIX
    from mmw.site.tracks import content_ident
    expected = pool.sent_slice(network, point)
    if expected is None:
        return None

    def choices():
        yield settings
        yield pool.settings_at(network, '99999999T999999Z')
        for bundle in pool.bundles(network):
            path = ledger_dir(pool.pool_dir.parent, network) / f'{bundle.to_stamp}{MANIFEST_SUFFIX}'
            try:
                with disk_io.ops.open_file(path, 'r', encoding='utf-8') as fh:
                    header = json.loads(fh.readline())
                    if isinstance(header, dict):
                        yield header.get('settings')
            except (OSError, ValueError):
                continue

    for candidate in choices():
        if not isinstance(candidate, str) or not candidate:
            continue
        try:
            files = files_for_settings(pool, point, candidate)
        except (ValueError, TypeError):
            continue
        if content_ident(files) == expected:
            return set(files.values())
    return None


_DIM_FIELDS = {'arch': 'arch', 'arches': 'arch', 'architecture': 'arch', 'architectures': 'arch',
               'component': 'component', 'components': 'component', 'suite': 'suite', 'suites': 'suite'}


def unit_key(mirror: dict) -> tuple:
    """One selected unit of a recorded selection, whatever its name."""
    return (mirror.get('source'), mirror.get('suite'), mirror.get('component'), mirror.get('arch'))


def _mirrors(settings) -> list[dict]:
    import json
    try:
        mirrors = json.loads(settings or '{}').get('mirrors', [])
    except (ValueError, AttributeError):
        return []
    return [m for m in mirrors if isinstance(m, dict)]


def whole_units(specs, settings, labels) -> frozenset:
    """The units `--whole` names. Each spec is `[REPO:]DIM=VALUE[,VALUE]`, e.g.
    `ubuntu:arches=i386`; REPO (a label or URL) defaults to every repository."""
    from mmw.core.repos import resolve_repo_refs
    mirrors = _mirrors(settings)
    out = set()
    for spec in specs or ():
        head, sep, values = spec.partition('=')
        ref, _, dim = head.rpartition(':')
        field = _DIM_FIELDS.get(dim.strip().lower())
        wanted = {v.strip() for v in values.split(',') if v.strip()}
        if not sep or field is None or not wanted:
            raise ValueError(f'send whole {spec!r}: expected [REPO:]arches|components|suites=VALUE[,VALUE]')
        repos = set(resolve_repo_refs([ref.strip()], labels)) if ref.strip() else None
        hit = {unit_key(m) for m in mirrors
               if (repos is None or m.get('source') in repos) and m.get(field) in wanted}
        if not hit:
            raise ValueError(f'send whole {spec!r}: matches nothing this network selects')
        out |= hit
    return frozenset(out)


def narrowed_selection(settings, *, kept=None, whole=frozenset()):
    """`settings` with only the units the far side is assumed to hold: not
    one `--whole` names, and (given a kept bundle's selection) one it carried.
    Returns (settings, dropped mirrors); unchanged settings when none drop."""
    import json
    mirrors = _mirrors(settings)
    carried = None if kept is None else {unit_key(m) for m in _mirrors(kept)}
    keep = [m for m in mirrors if unit_key(m) not in whole and (carried is None or unit_key(m) in carried)]
    if len(keep) == len(mirrors):
        return settings, []
    data = json.loads(settings)
    data['mirrors'] = keep
    return json.dumps(data, sort_keys=True), [m for m in mirrors if m not in keep]


def describe_whole(dropped, settings, label, why) -> list[str]:
    """One line per repository: what goes whole, by the dimension values
    dropped entirely (`arches i386`), else a count of units."""
    mirrors = _mirrors(settings)
    gone = {unit_key(m) for m in dropped}
    lines = []
    for repo in sorted({m.get('source') for m in dropped}):
        mine = [m for m in mirrors if m.get('source') == repo]
        lost = [m for m in mine if unit_key(m) in gone]
        parts = []
        for field, name in (('suite', 'suites'), ('component', 'components'), ('arch', 'arches')):
            every = {m.get(field) for m in mine}
            whole = sorted(v for v in {m.get(field) for m in lost} if v is not None and
                           all(unit_key(m) in gone for m in mine if m.get(field) == v))
            if whole and len(whole) < len(every):
                parts.append(f"{name} {', '.join(whole)}")
        what = '; '.join(parts) or ('everything' if len(lost) == len(mine) else f'{len(lost)} unit(s)')
        lines.append(f'{label(repo)}: {what} sent whole ({why})')
    return lines


def assumed_selection(network, mirror_dir, settings, *, cfg=None, since=None, kept_dirs=None, whole=None):
    """What the baseline projects with: (settings, notes). With an explicit
    From, a kept bundle at or before it says which units the far side was
    sent; `--whole` names units it lacks. Either only ever sends more."""
    from mmw.core.repos import repository_label
    label = (lambda key: repository_label(key, cfg)) if cfg is not None else str
    notes = []
    whole_keys = frozenset()
    if whole:
        from mmw.core.repos import network_repos
        labels = network_repos(network, cfg, mirror_dir) if cfg is not None else {}
        whole_keys = whole_units(whole, settings, labels)
        _, dropped = narrowed_selection(settings, whole=whole_keys)
        notes += describe_whole(dropped, settings, label, '--whole')
    kept = None
    if since is not None:
        kept = kept_state(network, mirror_dir, since, kept_dirs)
    if kept is not None and kept.settings:
        from pathlib import Path
        _, dropped = narrowed_selection(settings, kept=kept.settings, whole=whole_keys)
        dropped = [m for m in dropped if unit_key(m) not in whole_keys]
        notes += describe_whole(dropped, settings, label, f"not in {Path(kept.where).name}'s selection")
    narrowed, _ = narrowed_selection(settings, kept=kept.settings if kept else None, whole=whole_keys)
    return narrowed, notes


def kept_state(network, mirror_dir, since, kept_dirs=None):
    """The newest kept bundle of `network` at or before `since`, or None."""
    from mmw.core.publisher.kept import state_at
    from mmw.core.publisher.manifest import ledger_dir
    dirs = [*([network.bundle_dir] if network.bundle_dir is not None else []),
            ledger_dir(mirror_dir, network.name), *(kept_dirs or [])]
    return state_at(dirs, network.name, since, skipped=[])


def range_baseline(pool, network, settings, targets, *, since=None, full=False, listing=None,
                   base_settings=None):
    """Files guaranteed at every retained starting point inside X..Z.

    Subtracting only X loses a file removed at Y and reintroduced at Z.
    Intersect the selected slices throughout the interval instead. Missing
    history makes the safe baseline empty, so it never causes an omission.
    `base_settings` (see `assumed_selection`) narrows what the far side is
    assumed to hold; what it needs is still projected with `settings`.
    """
    base = base_settings or settings
    from mmw.core.points import resolve_when
    if not repository_owned(pool, targets, network):
        return None                      # compatibility with legacy network states
    if full:
        return set()
    known = pool.points_of(pid for ids in targets.values() for pid in ids)
    cursors = pool.sent_dates_by_track(network.name)
    explicit = resolve_when(since) if since else None
    common = set()
    needed_elsewhere = set()
    listed = pool.points_of(pid for ids in (listing or targets).values() for pid in ids)
    for tid in {p.track for p in known.values()}:
        target = next(p for p in known.values() if p.track == tid)
        # A layer edit can keep the upstream Release date. A historical
        # baseline cannot assume a layer built after From was already carried.
        layered = explicit is not None and network.layer_for(pool.track_of_point(target.id).repo) is not None

        def baseline_date(point):
            date = point_date(pool, point, settings)
            return max(date, point.stamp) if layered else date

        end = baseline_date(target)
        start = explicit or cursors.get(tid)
        needed = {cs for p in listed.values() if p.track == tid
                  for cs in files_for_settings(pool, p.id, settings).values()}
        if start is None:
            needed_elsewhere.update(needed)
            continue
        if start > end and explicit is None:
            needed_elsewhere.update(needed)
            continue                     # a past destination needs a full payload
        dated = [(baseline_date(p), p) for p in
                 retained_track_points(pool, network.name, tid, known, settings)]
        # A suite may be unchanged throughout the requested interval: its
        # Release date predates From, but it is still the state at both ends.
        before = [(date, p) for date, p in dated if date <= min(start, end)]
        if not before:
            needed_elsewhere.update(needed)
            continue
        first = max(before, key=lambda item: (item[0], item[1].stamp, item[1].ident))[1]
        prior_slice = transferred_slice(pool, network.name, first.id, settings)
        if prior_slice is None and (explicit is None or pool.sent_slice(network.name, first.id) is not None):
            needed_elsewhere.update(needed)
            continue
        interval = [first, *(p for date, p in dated if start < date <= end)]
        if any(not p.held for p in interval):
            needed_elsewhere.update(needed)
            continue
        held = [set(files_for_settings(pool, p.id, base).values()) for p in interval]
        if prior_slice is not None:
            held[0] = prior_slice if base_settings is None else prior_slice & held[0]
        if held:
            certain = set.intersection(*held)
            common.update(certain)
            needed_elsewhere.update(needed - certain)
    return common - needed_elsewhere


def settings_network(settings):
    import json
    return json.loads(settings).get('network', {}).get('name', '')


def validate_dates(since, until):
    from mmw.core.points import resolve_when
    if since and until and resolve_when(since) > resolve_when(until):
        raise ValueError('From must be at or before To — choose an earlier starting point.')


def select_range(pool, network, settings, current, *, until=None, history=False,
                 since=None, since_by_repo=None, explicit_since=False, sent_points=(), repos=None, warnings=None,
                 full=False):
    """(targets, listed history, points): never silently omit a selected track."""
    from mmw.core.points import resolve_when
    cutoff = resolve_when(until) if until else None
    selected = {r: list(ids) for r, ids in current.items()
                if r.startswith('network:') or repos is None or r in repos}
    if repos is not None:
        unknown = set(repos) - set(current)
        if unknown:
            raise ValueError(f"unknown repository {sorted(unknown)[0]!r}")
    known = pool.points_of(pid for ids in selected.values() for pid in ids)
    if cutoff:
        for repo, ids in list(selected.items()):
            if repo.startswith('network:'):
                continue
            targets = []
            for tid in sorted({known[p].track for p in ids}):
                candidates = retained_track_points(pool, network, tid, known, settings)
                dated = [(point_date(pool, p, settings), p.stamp, p.ident, p) for p in candidates]
                eligible = [item for item in dated if item[0] <= cutoff]
                track = pool.track_of_point(next(pid for pid in ids if known[pid].track == tid))
                if not eligible:
                    raise ValueError(f"no point for {repo} / {track.suite or track.name or 'files'} "
                                     f"at or before {until} — choose a later date or exclude this repository")
                point = max(eligible, key=lambda item: item[:3])[3]
                if not point.held:
                    raise ValueError(f"{repo} / {track.suite or track.name}: point at or before {until} "
                                     "is no longer kept (retention dropped its files)")
                targets.append(point.id)
                known[point.id] = point
            selected[repo] = targets
    listing = {}
    cursors = pool.transfer_dates(network)
    for repo, ids in selected.items():
        if repo.startswith('network:'):
            continue
        listing[repo] = list(ids)
        if not history:
            continue
        start = (since_by_repo or {}).get(repo, since) if explicit_since else (since_by_repo or {}).get(repo)
        gaps = 0
        for tid in sorted({known[p].track for p in ids}):
            # Imported states may be newer than the live current point.
            # Carrying history must not silently advance past the selected To.
            upper = (max(point_date(pool, known[p], settings) for p in ids if known[p].track == tid)
                     if any(not known[p].legacy for p in ids if known[p].track == tid) else None)
            for p in retained_track_points(pool, network, tid, known, settings):
                first = pool.network_point_stamp(network, p.id) or p.stamp
                after_start = start is None or first > start
                if not p.legacy:
                    lower = None if full else since if explicit_since else cursors.get(tid)
                    after_start = lower is None or point_date(pool, p, settings) > lower
                    if not explicit_since:
                        # Newly imported or explicitly included older history
                        # still rides the next default history transfer.
                        after_start = after_start or start is None or first > start
                elif not explicit_since and tid in cursors:
                    after_start = after_start or point_date(pool, p, settings) > cursors[tid]
                if (p.id not in listing[repo] and (not p.legacy or explicit_since or p.id not in sent_points)
                        and after_start
                        and (upper is None or point_date(pool, p, settings) <= upper)
                        and (cutoff is None or point_date(pool, p, settings) <= cutoff)):
                    if not p.held:
                        gaps += 1
                        continue
                    listing[repo].append(p.id)
                    known[p.id] = p
        if gaps and warnings is not None:
            warnings.append(f"{repo}: {gaps} historical point(s) are no longer kept; not included in the range")
    stubs = [p for ids in [*listing.values(), selected.get(f"network:{network}", [])]
             for p in ids if not known[p].held]
    if stubs:
        raise ValueError(f"{len(stubs)} point(s) to list are no longer kept in the pool "
                         "(retention dropped their files) — choose a later range or newest points only")
    return selected, listing, known


@dataclass(frozen=True)
class RangeRow:
    repo: str
    suite: str
    from_date: str | None
    to_date: str
    points: int
    available: tuple[str, ...]


@dataclass(frozen=True)
class TransferPreview:
    rows: tuple[RangeRow, ...]
    blobs: int
    bytes: int
    source: str
    warnings: tuple[str, ...] = ()
    whole: tuple[str, ...] = ()     # what goes in full whatever From says (see `assumed_selection`)


def available_transfer_dates(net, mirror_dir, *, cfg=None):
    """Retained endpoint dates for the network's configured repository slices."""
    from mmw.core.pool import Pool
    from mmw.core.points import prepare_history
    prepare_history(mirror_dir)
    with Pool(mirror_dir, read_only=True) as pool:
        settings, current = recorded_selection(pool, net, mirror_dir, cfg)
        known = pool.points_of(pid for ids in current.values() for pid in ids)
        return tuple(sorted({point_date(pool, point, settings)
                             for tid in {p.track for p in known.values()}
                             for point in retained_track_points(pool, net.name, tid, known, settings)
                             if point.held}, reverse=True))


def preview_transfer(net, mirror_dir, *, cfg=None, since=None, until=None, full=False,
                     history=None, repos=None, kept_dirs=None, whole=None):
    """Resolve the same retained point range and checksum baseline as a transfer."""
    from mmw.core import points
    from mmw.core.pool import Pool
    from mmw.core.publisher.bundle import _far_nginx, resolve_since
    from mmw.site.tracks import content_ident
    import hashlib
    validate_dates(since, until)
    points.prepare_history(mirror_dir)

    baseline, source, warnings = None, 'last transfer (local record)', []
    if since is not None:
        since_stamp, baseline, source, warnings = resolve_since(net, mirror_dir, since, kept_dirs, cfg=cfg,
                                                                repos=repos, whole=whole)
    else:
        since_stamp = None
    with Pool(mirror_dir, read_only=True) as pool:
        settings, current = recorded_selection(pool, net, mirror_dir, cfg, only=repos)
        extras, external = {}, {}
        if cfg is not None and repository_owned(pool, current, net):
            _, settings, extras, notes, external = selected_tree(
                pool, net, cfg, mirror_dir, settings, current, until=until, preview=True)
            warnings.extend(notes)
        by_repo, fallback = points.history_since(pool, net.name)
        if since is None and fallback is None:
            source = "first transfer — send everything"
        sent_points = {p for p in pool.sent_point_ids(net.name)
                       if pool.sent_slice(net.name, p) in
                       (None, content_ident(files_for_settings(pool, p, settings)))}
        targets, listing, known = select_range(
            pool, net.name, settings, current, until=until,
            history=net.history if history is None else history,
            since=since_stamp if since is not None else fallback,
            since_by_repo={} if since is not None or full else by_repo,
            explicit_since=since is not None, sent_points=() if full else sent_points, repos=repos, warnings=warnings,
            full=full)
        from mmw.core.legacy_points import notes as legacy_notes
        warnings.extend(legacy_notes(pool, current))
        files = {p: files_for_settings(pool, p, settings) for ids in listing.values() for p in ids}
        for p in targets.get(f'network:{net.name}', []):
            files[p] = pool.point_files(p)
        checksums = {cs for f in files.values() for cs in f.values()}
        checksums.update(extras.values())
        info = pool.blob_info(checksums)
        info.update(external)
        if checksums - info.keys():
            raise ValueError('Some files of the selected points are no longer in the pool — verify or choose another range.')
        generated = _far_nginx(settings, net.certs)
        generated = {hashlib.sha256(data).hexdigest(): len(data) for data in dict(generated).values()}
        sent = baseline if baseline is not None else set() if full else pool.sent_checksums(net.name, checksums | generated.keys())
        base, whole_notes = (settings, []) if full else assumed_selection(
            net, mirror_dir, settings, cfg=cfg, since=since_stamp, kept_dirs=kept_dirs, whole=whole)
        interval_base = range_baseline(pool, net, settings, targets, since=since, full=full, listing=listing,
                                       base_settings=None if base is settings else base)
        if interval_base is not None:
            # Both are what the far side is assumed to hold: trust only what both say.
            sent = interval_base if baseline is None else interval_base & baseline
        sizes = {cs: data[1] for cs, data in info.items()}
        sizes.update(generated)
        payload = sizes.keys() - sent
        before = pool.sent_dates_by_track(net.name)
        rows = []
        for repo, ids in listing.items():
            for tid in sorted({known[p].track for p in targets[repo]}):
                point = next(known[p] for p in targets[repo] if known[p].track == tid)
                track = pool.track_of_point(point.id)
                retained = retained_track_points(pool, net.name, tid, known, settings)
                available = tuple(sorted({point_date(pool, p, settings)
                                          for p in retained if p.held}))
                to_date = point_date(pool, point, settings)
                from_date = None if full else since_stamp if since else before.get(tid)
                if since and not full:
                    eligible = [point_date(pool, p, settings) for p in retained
                                if point_date(pool, p, settings) <= min(since_stamp, to_date)]
                    from_date = max(eligible, default=since_stamp)
                rows.append(RangeRow(repo, track.suite or track.name.rsplit('/', 1)[-1] or 'files',
                                     from_date, to_date,
                                     sum(known[p].track == tid for p in ids), available))
        return TransferPreview(tuple(rows), len(payload), sum(sizes[c] for c in payload),
                               'everything' if full else source, tuple(warnings), tuple(whole_notes))
