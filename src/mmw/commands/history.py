"""Read repository history without attaching it to a network."""
from dataclasses import dataclass
import json

from mmw.core.pool import Pool


@dataclass(frozen=True)
class HistoryRow:
    repo: str
    suite: str
    date: str
    held: bool
    current: bool
    ident: str
    legacy: bool


def prepare(cfg, *, network=None, repos=None, since=None):
    from mmw.core.points import prepare_recordings, resolve_when
    from mmw.core.repository_points import current_repository_points, selected_point_files, wire_draft
    from mmw.site.tracks import content_ident
    from mmw.core.resolve import available_mirrors, network_mirrors
    from mmw.core.repos import repository_key, repository_label, resolve_repo_refs
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        upgrade = not pool._has_table('history_upgrades')
    if upgrade:
        with Pool(cfg.settings.mirror_dir):
            pass  # Complete the one-time storage upgrade, never a fresh sync.
    if network is not None:
        net = next((n for n in cfg.networks if n.name == network), None)
        if net is None:
            raise LookupError(f'unknown network {network!r}')
        units = network_mirrors(net, cfg, cfg.settings.mirror_dir)
    else:
        units = list({u.name: u for u in [*cfg.mirrors, *available_mirrors(cfg, cfg.settings.mirror_dir)]}.values())
    names = {repository_key(u): repository_label(repository_key(u), cfg) for u in units}
    chosen = resolve_repo_refs(list(repos), names) if repos else sorted(names)
    cutoff = resolve_when(since)[:8] + 'T000000Z' if since and 'T' not in since.upper() else (
        resolve_when(since) if since else None)
    # One-time compatibility conversion reads old retained states, never live
    # refs: a history view must not require another sync after upgrading.
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        convert = [name for name in pool.network_names()
                   if pool.snapshots(name) and not pool.points_converted(name)]
    for name in convert:
        prepare_recordings(cfg.settings.mirror_dir, name)
    rows = []
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        for repo in chosen:
            current = set(current_repository_points(pool, repo))
            seen = set()
            with pool._read() as c:
                ids = [r[0] for r in c.execute('SELECT p.id FROM points p JOIN tracks t ON t.id = p.track '
                                               'WHERE t.repo = ?', (repo,))]
            for point in sorted(pool.points_of(ids).values(),
                                key=lambda p: (p.id in current, p.date, p.stamp, p.id), reverse=True):
                track = pool.track_of_point(point.id)
                mine = [u for u in units if repository_key(u) == repo]
                if track.name and not any(not u.suite or track.name == u.mirror_base + '/dists/' + u.suite
                                          or track.name == u.mirror_base for u in mine):
                    continue
                date = point.date
                if not point.legacy and point.held:
                    files = selected_point_files(pool, point.id, mine)
                    if not files:
                        continue
                    identity = (point.track, content_ident(files))
                    if identity in seen:
                        continue
                    seen.add(identity)
                    settings = json.dumps({'repos': {repo: sorted({u.mirror_base for u in mine})}})
                    date = wire_draft(pool, point.id, files, settings, point.stamp).date
                if cutoff and date < cutoff:
                    continue
                rows.append(HistoryRow(repo, track.suite or track.name or 'files', date,
                                       point.held, point.id in current, point.ident, point.legacy))
    return tuple(sorted(rows, key=lambda r: (r.repo, r.suite, r.date, r.ident), reverse=True))


def describe(cfg, rows):
    from mmw.core.repos import repository_label
    from mmw.site.versionref import day
    return '\n'.join(f'{repository_label(r.repo, cfg)} / {r.suite}: {day(r.date)} '
                     f'{r.date[9:15]} UTC · {"kept" if r.held else "no longer kept"}'
                     + (' · latest sync' if r.current else '')
                     + (' · legacy history' if r.legacy else '') for r in rows) or 'No repository points recorded yet.'


def run(cfg_dir, *, network=None, repos=None, since=None):
    from mmw.commands.common import eprint, load_config_or_none
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1
    try:
        print(render(cfg, network=network, repos=repos, since=since))
    except (ValueError, LookupError, OSError) as exc:
        eprint(str(exc))
        return 1
    return 0


def render(cfg, **selection):
    rows = prepare(cfg, **selection)
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        notes = pool.legacy_publish_notes()
    return '\n'.join([describe(cfg, rows), *notes])
