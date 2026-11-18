"""Include retained repository history without changing a network's present."""
from __future__ import annotations

import json
from dataclasses import dataclass

from mmw.core.pool import Pool, exclusive_pool_lock


@dataclass(frozen=True)
class EarlierPoint:
    id: int
    repo: str
    suite: str
    date: str


@dataclass(frozen=True)
class HistoryPlan:
    network: str
    since: str | None
    repos: tuple[str, ...]
    signature: str
    points: tuple[EarlierPoint, ...]
    notes: tuple[str, ...]


def _selection(cfg, name):
    from mmw.core.resolve import network_mirrors
    net = next((n for n in cfg.networks if n.name == name), None)
    if net is None:
        raise LookupError(f'unknown network {name!r}')
    units = network_mirrors(net, cfg, cfg.settings.mirror_dir)
    signature = json.dumps([net.model_dump(mode='json'), sorted(
        (u.source, sorted(u.dims.items())) for u in units)], sort_keys=True)
    return net, units, signature


def prepare(cfg, network, since=None, repos=None) -> HistoryPlan:
    """Look: an immutable preview, including unavailable/stubbed history."""
    from mmw.core.points import resolve_when
    from mmw.core.repository_points import selected_point_files
    from mmw.site.tracks import content_ident
    from mmw.core.repos import repository_key, repository_label, resolve_repo_refs
    net, units, signature = _selection(cfg, network)
    names = {repository_key(u): repository_label(repository_key(u), cfg) for u in units}
    chosen = resolve_repo_refs(list(repos), names) if repos else sorted(names)
    cutoff = resolve_when(since) if since else None
    if cutoff and 'T' not in since.upper():
        cutoff = cutoff[:8] + 'T000000Z'
    found, notes = [], []
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        linked_points = pool.network_points(network, stubs=True)
        linked = {p.id for p in linked_points}
        seen = {(p.track, content_ident(selected_point_files(pool, p.id, units)))
                for p in linked_points if p.held}
        for repo in chosen:
            if net.layer_for(repo) is not None:
                notes.append(f'{names[repo]}: {network} has a layer — earlier upstream points skipped')
                continue
            mine = [u for u in units if repository_key(u) == repo]
            for p in pool.repository_points(repo, stubs=True):
                if p.id in linked or (cutoff and p.date < cutoff):
                    continue
                track = pool.track_of_point(p.id)
                if track.name and not any(not u.suite or track.name == u.mirror_base + '/dists/' + u.suite for u in mine):
                    continue
                if not p.held:
                    notes.append(f'{names[repo]} {track.suite or track.name}: {p.date[:8]} no longer kept — cannot include')
                    continue
                files = selected_point_files(pool, p.id, units)
                identity = (p.track, content_ident(files))
                if not files or identity in seen:
                    continue
                seen.add(identity)
                found.append(EarlierPoint(p.id, repo, track.suite or track.name or 'files', p.date))
    return HistoryPlan(network, since, tuple(chosen), signature, tuple(found), tuple(notes))


def include(cfg, plan: HistoryPlan) -> int:
    """Add exactly the looked-at retained points, revalidated under the lock."""
    from mmw.core.publisher.manifest import bump_stamp, now_stamp
    from mmw.core.repository_points import selected_point_files, cache_slice
    md = cfg.settings.mirror_dir
    with exclusive_pool_lock(md, 'include earlier points'), Pool(md) as pool:
        _net, units, signature = _selection(cfg, plan.network)
        if signature != plan.signature:
            raise ValueError('Network selection changed. Look again before adding earlier points.')
        known = pool.points_of(p.id for p in plan.points)
        if any(p.id not in known or not known[p.id].held for p in plan.points):
            raise ValueError('Some points are no longer kept. Look again before adding earlier points.')
        stamp = now_stamp()
        bundles = pool.bundles(plan.network)
        if bundles:
            stamp = max(stamp, bump_stamp(bundles[0].to_stamp))
        added = 0
        for point in plan.points:
            files = selected_point_files(pool, point.id, units)
            if files and pool.network_point_stamp(plan.network, point.id) is None:
                pool.link_network_point(plan.network, point.id, stamp)
                cache_slice(pool, plan.network, point.id, files, units)
                added += 1
    return added


def describe(cfg, plan):
    from collections import Counter
    from mmw.core.repos import repository_label
    counts = Counter((p.repo, p.suite) for p in plan.points)
    lines = [f'{repository_label(repo, cfg)} {suite}: {count} earlier point(s)'
             for (repo, suite), count in sorted(counts.items())]
    return '\n'.join([*(lines or ['No retained earlier points to include.']), *plan.notes])


def run(cfg_dir, network, since=None, repos=None, dry_run=False):
    from mmw.commands.common import eprint, load_config_or_none
    from mmw.core.pool import PoolBusy
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1
    try:
        plan = prepare(cfg, network, since, repos)
        print(describe(cfg, plan))
        if not dry_run:
            print(f'Included {include(cfg, plan)} earlier point(s) in {network}.')
    except (ValueError, LookupError, OSError, PoolBusy) as e:
        eprint(str(e))
        return 1
    return 0
