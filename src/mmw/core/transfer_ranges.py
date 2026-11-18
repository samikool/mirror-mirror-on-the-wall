"""Read-only point selection shared by transfer preview and bundle writing."""
from dataclasses import dataclass

from mmw.core.repository_points import files_for_settings, wire_draft


def point_date(pool, point, settings):
    if point.legacy:
        return point.date
    if not point.held:
        return pool.network_slice_date(settings_network(settings), point.id) or point.date
    files = files_for_settings(pool, point.id, settings)
    return wire_draft(pool, point.id, files, settings, point.stamp).date if files else point.date


def settings_network(settings):
    import json
    return json.loads(settings).get('network', {}).get('name', '')


def select_range(pool, network, settings, current, *, until=None, history=False,
                 since=None, since_by_repo=None, explicit_since=False, sent_points=(), repos=None, warnings=None):
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
                candidates = pool.network_points(network, tid, stubs=True)
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
            for p in pool.network_points(network, tid, stubs=True):
                first = pool.network_point_stamp(network, p.id) or p.stamp
                after_start = start is None or first > start
                if not explicit_since and tid in cursors:
                    after_start = after_start or point_date(pool, p, settings) > cursors[tid]
                if (p.id not in listing[repo] and (explicit_since or p.id not in sent_points)
                        and after_start
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


def preview_transfer(net, mirror_dir, *, since=None, until=None, full=False,
                     history=None, repos=None, kept_dirs=None):
    """Resolve the same retained point range and checksum baseline as a transfer."""
    from mmw.core import points
    from mmw.core.pool import Pool
    from mmw.core.publisher.bundle import _far_nginx, resolve_since
    from mmw.site.tracks import content_ident
    import hashlib

    baseline, source, warnings = None, 'last transfer (local record)', []
    if since is not None:
        since_stamp, baseline, source, warnings = resolve_since(net, mirror_dir, since, kept_dirs)
    else:
        since_stamp = None
    with Pool(mirror_dir, read_only=True) as pool:
        settings = pool.settings_at(net.name, '99999999T999999Z')
        if not settings:
            raise ValueError('No recorded points for this network — sync its repositories first.')
        current = points.current_points(pool, net.name)
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
            explicit_since=since is not None, sent_points=() if full else sent_points, repos=repos, warnings=warnings)
        files = {p: files_for_settings(pool, p, settings) for ids in listing.values() for p in ids}
        for p in targets.get(f'network:{net.name}', []):
            files[p] = pool.point_files(p)
        checksums = {cs for f in files.values() for cs in f.values()}
        info = pool.blob_info(checksums)
        if checksums - info.keys():
            raise ValueError('Some files of the selected points are no longer in the pool — verify or choose another range.')
        generated = _far_nginx(settings, net.certs)
        generated = {hashlib.sha256(data).hexdigest(): len(data) for data in dict(generated).values()}
        sent = baseline if baseline is not None else set() if full else pool.sent_checksums(net.name, checksums | generated.keys())
        sizes = {cs: data[1] for cs, data in info.items()}
        sizes.update(generated)
        payload = sizes.keys() - sent
        before = pool.sent_dates_by_track(net.name)
        rows = []
        for repo, ids in listing.items():
            for tid in sorted({known[p].track for p in targets[repo]}):
                point = next(known[p] for p in targets[repo] if known[p].track == tid)
                track = pool.track_of_point(point.id)
                available = tuple(sorted({point_date(pool, p, settings)
                                          for p in pool.network_points(net.name, tid) if p.held}))
                rows.append(RangeRow(repo, track.suite or track.name.rsplit('/', 1)[-1] or 'files',
                                     None if full else since_stamp if since else before.get(tid),
                                     point_date(pool, point, settings),
                                     sum(known[p].track == tid for p in ids), available))
        return TransferPreview(tuple(rows), len(payload), sum(sizes[c] for c in payload),
                               'everything' if full else source, tuple(warnings))
