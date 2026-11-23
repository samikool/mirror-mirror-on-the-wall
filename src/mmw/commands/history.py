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
    point: int = 0


def prepare(cfg, *, network=None, repos=None, since=None):
    from mmw.core.points import prepare_history, prepare_recordings, resolve_when
    from mmw.core.repository_points import current_repository_points, selected_point_files, wire_draft
    from mmw.site.tracks import content_ident
    from mmw.core.resolve import available_mirrors, network_mirrors
    from mmw.core.repos import repository_key, repository_label, resolve_repo_refs
    prepare_history(cfg.settings.mirror_dir)
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
                                       point.held, point.id in current, point.ident, point.legacy, point.id))
    return tuple(sorted(rows, key=lambda r: (r.repo, r.suite, r.date, r.ident), reverse=True))


def describe(cfg, rows):
    from mmw.core.repos import repository_label
    from mmw.site.versionref import day_time
    return '\n'.join(f'{repository_label(r.repo, cfg)} / {r.suite}: {day_time(r.date)} '
                     f'· {"kept" if r.held else "no longer kept"}'
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


# ── deleting points by hand ──────────────────────────────────────────────────

@dataclass(frozen=True)
class DeleteItem:
    repo: str
    suite: str
    date: str                  # the stamp `mmw history` shows
    point: int
    frees: int                 # what deleting it alone frees
    protected: str | None      # why it is kept unless asked again (--yes)


def _matches(text: str, before: bool):
    """A test on a row's date for WHEN: that day (or that exact stamp), or
    everything before it."""
    from mmw.core.points import resolve_when
    end = resolve_when(text)
    exact = 'T' in text.upper() and len(text.strip()) > 9
    start = end if exact else end[:8] + 'T000000Z'
    if before:
        return lambda date: date < start
    return (lambda date: date == end) if exact else (lambda date: date[:8] == end[:8])


def _why_kept(row, published, unsent) -> str | None:
    reasons = []
    if row.current:
        reasons.append('the latest sync')
    reasons += [f'published at {root}' for root in published.get(row.point, ())]
    reasons += [f'not yet sent to {name} (history = true)' for name, ids in unsent.items() if row.point in ids]
    return '; '.join(reasons) or None


def delete_plan(pool, cfg, repo, when, suite=None, before=False) -> list[DeleteItem]:
    """The kept points of `repo` dated `when` (or before it, with `before`),
    optionally in one suite, each with what it frees and why it is protected."""
    import sqlite3
    from mmw.core.points import unsent_points
    matches = _matches(when, before)
    rows = [r for r in prepare(cfg, repos=[repo]) if r.held and matches(r.date)]
    if suite is not None:
        def names(r):
            t = pool.track_of_point(r.point)
            return {r.suite, t.suite, t.codename, t.name, t.name.rsplit('/', 1)[-1] if t.name else None}
        rows = [r for r in rows if suite in names(r)]
    with pool._read() as c:
        try:
            published = {}
            for root, pid in c.execute('SELECT root, point FROM published_points'):
                published.setdefault(pid, []).append(root)
        except sqlite3.OperationalError:          # a pool from before published points
            published = {}
    unsent = {n.name: unsent_points(pool, n) for n in cfg.networks if n.history}
    return [DeleteItem(r.repo, r.suite, r.date, r.point, pool.points_frees([r.point]),
                       _why_kept(r, published, unsent)) for r in rows]


def describe_plan(cfg, items) -> str:
    """The listing both `mmw history delete` and the TUI show before deleting."""
    from mmw.core.format import fmt_size
    from mmw.core.repos import repository_label
    from mmw.site.versionref import day_time
    if not items:
        return 'No kept point matches.'
    lines = [f'{repository_label(i.repo, cfg)} / {i.suite}: {day_time(i.date)}'
             f' · frees {fmt_size(i.frees)}' + (f' · protected: {i.protected}' if i.protected else '')
             for i in items]
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        total = pool.points_frees([i.point for i in items])
    lines.append(f'{len(items)} point(s) · {fmt_size(total)} freed together · their records stay in history')
    return '\n'.join(lines)


def delete(cfg, items, progress=None) -> int:
    """Stub `items` and free the files nothing else wants, under the prune's
    lock; bytes freed. No ref is dropped: every repository the pool has refs
    for counts as configured here, so only files nothing wants go."""
    pool = Pool(cfg.settings.mirror_dir)
    try:
        sources = sorted({*pool.ref_sources(), *(s.identity for s in cfg.sources)}) or ['']
        ids = [i.point for i in items]

        def stub(locked):
            for pid in ids:
                locked.stub_point(pid)
        return pool.prune_orphans(sources, (), progress=progress, before=stub)[1]
    finally:
        pool.close()


def run_delete(cfg_dir, repo, when, *, suite=None, before=False, yes=False, dry_run=False) -> int:
    import sqlite3
    import sys
    from mmw.commands.common import eprint, load_config_or_none
    from mmw.core.format import fmt_size
    from mmw.core.pool import PoolBusy
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1
    try:
        with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
            items = delete_plan(pool, cfg, repo, when, suite=suite, before=before)
        listing = describe_plan(cfg, items)
    except (ValueError, LookupError, OSError, sqlite3.Error) as exc:
        eprint(str(exc))
        return 1
    if not items:
        eprint(f'no kept point of {repo} is dated {"before " if before else ""}{when}'
               + (f' in {suite}' if suite else '') + ' — see mmw history')
        return 1
    print(listing)
    if dry_run:
        print('(dry run — nothing deleted)')
        return 0
    protected = [i for i in items if i.protected]
    if protected and not yes:
        eprint(f'{len(protected)} of these are protected — pass --yes to delete them anyway')
        return 1
    if not yes:
        if not sys.stdin.isatty():
            eprint('pass --yes to delete them (no terminal to ask on)')
            return 1
        if input(f'Delete {len(items)} point(s)? [y/N] ').strip().lower() not in ('y', 'yes'):
            print('Aborted — nothing was deleted.')
            return 1
    try:
        freed = delete(cfg, items)
    except (PoolBusy, OSError, sqlite3.Error) as exc:
        eprint(str(exc))
        return 1
    print(f'deleted {len(items)} point(s); freed {fmt_size(freed)} (their records stay in history)')
    return 0
