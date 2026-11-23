"""Read repository history without attaching it to a network."""
from dataclasses import dataclass
import json

from mmw.core.pool import Pool


@dataclass(frozen=True)
class HistoryRow:
    """One point, as both sides' history tables and `mmw history` show it.

    `marks` ⊆ {current, published:ROOT, sent:NETWORK, unsent:NETWORK, drops,
    labelled, inconsistent}. `size`: the point's distinct files; `frees`:
    what deleting it alone frees (0 for a stub)."""
    repo: str
    suite: str
    date: str
    ident: str
    held: bool
    size: int
    frees: int
    marks: tuple[str, ...]
    label: str | None = None
    point: int = 0
    legacy: bool = False

    @property
    def current(self) -> bool:
        return 'current' in self.marks


def _published(pool) -> dict[int, list[str]]:
    import sqlite3
    out: dict[int, list[str]] = {}
    with pool._read() as c:
        try:
            for root, pid in c.execute('SELECT root, point FROM published_points ORDER BY root'):
                out.setdefault(pid, []).append(root)
        except sqlite3.OperationalError:          # a pool from before published points
            pass
    return out


def _consistent(pool, point, track) -> bool:
    """The Release agrees with every index the point keeps (one read)."""
    from mmw.core.point_check import release_mismatches
    from mmw.core.points import _reader
    if point.legacy or not point.held or not track.name or '/dists/' not in track.name:
        return True
    files = pool.point_files(point.id)
    return not release_mismatches(files, track.name, _reader(pool, files))


def carried_by(cfg) -> dict[str, set[str]]:
    """Network name → the repositories it carries."""
    from mmw.core.repos import repository_key
    from mmw.core.resolve import network_mirrors
    return {n.name: {repository_key(u) for u in network_mirrors(n, cfg, cfg.settings.mirror_dir)}
            for n in cfg.networks}


def prepare(cfg, *, network=None, repos=None, since=None):
    from mmw.core.points import prepare_history, prepare_recordings, resolve_when, retention_plan, unsent_points
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
    # Which networks carry each repository: only theirs say sent / unsent.
    carried = carried_by(cfg)
    rows = []
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        published = _published(pool)
        drops = set(retention_plan(pool, cfg.networks, cfg=cfg))
        owned = {p.id: pool.track_of_point(p.id).repo for p in pool.repository_points()}
        sent = {n.name: (pool.sent_point_ids(n.name), pool.sent_dates_by_track(n.name),
                         unsent_points(pool, n, owned)) for n in cfg.networks}
        for repo in chosen:
            current = set(current_repository_points(pool, repo))
            seen = set()
            with pool._read() as c:
                ids = [r[0] for r in c.execute('SELECT p.id FROM points p JOIN tracks t ON t.id = p.track '
                                               'WHERE t.repo = ?', (repo,))]
            found = []
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
                found.append((point, track, date))
            sizes = pool.points_sizes([p.id for p, _, _ in found])
            for point, track, date in found:
                marks = []
                if point.id in current:
                    marks.append('current')
                marks += [f'published:{root}' for root in published.get(point.id, ())]
                for name, (ids_sent, dates, waiting) in sent.items():
                    if repo not in carried[name]:
                        continue
                    last = dates.get(point.track)
                    if point.id in ids_sent or (last is not None and point.date <= last):
                        marks.append(f'sent:{name}')
                    elif point.id in waiting or point.id in current:
                        marks.append(f'unsent:{name}')
                if point.id in drops:
                    marks.append('drops')
                if not _consistent(pool, point, track):
                    marks.append('inconsistent')
                rows.append(HistoryRow(repo, track.suite or track.name or 'files', date, point.ident,
                                       point.held, sizes.get(point.id, 0),
                                       pool.points_frees([point.id]) if point.held else 0,
                                       tuple(marks), None, point.id, point.legacy))
    # Grouped by repository and suite; newest first within each.
    rows.sort(key=lambda r: (r.date, r.ident), reverse=True)
    rows.sort(key=lambda r: (repository_label(r.repo, cfg), r.repo, r.suite))
    return tuple(rows)


def mark_text(mark: str, row: HistoryRow | None = None, *, short: bool = False) -> str:
    """A mark in words, as `mmw history` prints it (`short`: a TUI table's
    cell, a root by its directory's name)."""
    from mmw.core.format import fmt_size
    kind, _, arg = mark.partition(':')
    if short and kind == 'published':
        from pathlib import PurePath
        arg = PurePath(arg).name or arg
    return {'current': 'latest sync',
            'published': f'published at {arg}',
            'sent': f'sent to {arg}',
            'unsent': f'not yet sent to {arg}',
            'drops': 'pruned next' + (f' (frees {fmt_size(row.frees)})' if row and row.frees else ''),
            'labelled': f'labelled {row.label}' if row and row.label else 'labelled',
            'inconsistent': 'inconsistent — see mmw verify --points'}.get(kind, mark)


def describe(cfg, rows, *, network=None):
    """`mmw history`: per repository a heading with its summary, then
    `suite  date  size  marks`, newest first. With `network`, first what its
    far side has."""
    from mmw.core.format import fmt_size
    from mmw.core.repos import repository_label
    from mmw.site.versionref import day_time
    if not rows:
        return 'No repository points recorded yet.'
    width = max(len(r.suite) for r in rows)
    lines, last_repo, last_suite = [], None, None
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        summaries = {repo: repo_summary(cfg, repo, rows, pool) for repo in {r.repo for r in rows}}
        if network is not None:
            lines += [f'{network}: far side has {far_side_summary(network, rows, pool)}', '']
    for r in rows:
        if r.repo != last_repo:
            if last_repo is not None:
                lines.append('')
            label = repository_label(r.repo, cfg)
            lines.append((label if label == r.repo else f'{label} ({r.repo})') + f' — {summaries[r.repo]}')
            last_repo, last_suite = r.repo, None
        words = [mark_text(m, r) for m in r.marks]
        if not r.held:
            words.append('no longer kept')
        if r.legacy:
            words.append('legacy history')
        size = fmt_size(r.size) if r.held else '—'
        suite = r.suite if r.suite != last_suite else ''
        last_suite = r.suite
        lines.append(f'  {suite:<{width}}  {day_time(r.date)}  {size:>9}  {" · ".join(words)}'.rstrip())
    return '\n'.join(lines)


# ── one-line summaries: the Sources and Networks panes, `mmw history`'s headings

def _suites(pairs, limit=3) -> str:
    """'jammy 20 Nov, jammy-updates 21 Nov' (+N more) from (suite, date) pairs."""
    from mmw.site.versionref import short_day
    out = [f'{suite} {short_day(date)}' for suite, date in pairs]
    return ', '.join(out[:limit]) + (f' +{len(out) - limit} more' if len(out) > limit else '')


def _newest(rows) -> list[tuple[str, str]]:
    newest: dict[str, str] = {}
    for r in rows:
        if r.date > newest.get(r.suite, ''):
            newest[r.suite] = r.date
    return sorted(newest.items())


def keep_text(cfg, repo) -> str:
    """'keep all (default)', or the repository's own rule."""
    source = next((s for s in cfg.sources if s.identity == repo), None)
    own = getattr(source, 'keep_points', None) if source is not None else None
    return f'keep {own}' if own is not None else f'keep {cfg.settings.keep_points} (default)'


def repo_summary(cfg, repo, rows, pool) -> str:
    """'jammy 20 Nov · 4 kept · 18.0 KB · keep all (default)': each suite's
    newest kept point, how many are kept and what they take together."""
    from mmw.core.format import fmt_size
    kept = [r for r in rows if r.repo == repo and r.held]
    if not kept:
        return 'nothing recorded yet · ' + keep_text(cfg, repo)
    return (f'{_suites(_newest(kept))} · {len(kept)} kept · '
            f'{fmt_size(pool.points_footprint([r.point for r in kept]))} · {keep_text(cfg, repo)}')


def far_side_summary(network, rows, pool) -> str:
    """'jammy 01 Nov · newer here: 20 Nov (1 point, 14.0 KB)': each suite's
    newest point sent to `network`, then what is waiting to be sent and the
    bytes it was never sent. 'nothing yet' before any transfer."""
    from mmw.core.format import fmt_size
    from mmw.site.versionref import short_day
    sent = [r for r in rows if f'sent:{network}' in r.marks]
    waiting = [r for r in rows if f'unsent:{network}' in r.marks and r.held]
    text = _suites(_newest(sent)) if sent else 'nothing yet'
    if waiting:
        size = pool.points_footprint([r.point for r in waiting], unsent_to=network)
        text += (f' · newer here: {short_day(max(r.date for r in waiting))} '
                 f'({len(waiting)} point{"s" if len(waiting) != 1 else ""}, {fmt_size(size)})')
    return text


def repo_line(cfg, repo) -> str:
    """The Sources pane's History line for one repository."""
    rows = prepare(cfg, repos=[repo])
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        return repo_summary(cfg, repo, rows, pool)


def far_side_line(cfg, network) -> str:
    """The Networks pane's Far side has line for one network."""
    rows = prepare(cfg, network=network)
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        return far_side_summary(network, rows, pool)


def as_json(rows) -> str:
    from dataclasses import asdict
    return json.dumps([{**asdict(r), 'marks': list(r.marks)} for r in rows], indent=2)


def run(cfg_dir, *, network=None, repos=None, since=None, as_json_output=False):
    from mmw.commands.common import eprint, load_config_or_none
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1
    try:
        if as_json_output:
            print(as_json(prepare(cfg, network=network, repos=repos, since=since)))
        else:
            print(render(cfg, network=network, repos=repos, since=since))
    except (ValueError, LookupError, OSError) as exc:
        eprint(str(exc))
        return 1
    return 0


def render(cfg, **selection):
    rows = prepare(cfg, **selection)
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        notes = pool.legacy_publish_notes()
    return '\n'.join([describe(cfg, rows, network=selection.get('network')), *notes])


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


def _why_kept(row, unsent) -> str | None:
    reasons = []
    if row.current:
        reasons.append('the latest sync')
    reasons += [f'published at {m.partition(":")[2]}' for m in row.marks if m.startswith('published:')]
    reasons += [f'not yet sent to {name} (history = true)' for name, ids in unsent.items() if row.point in ids]
    return '; '.join(reasons) or None


def _in_suite(pool, row, suite) -> bool:
    """`suite` names the row's suite: its Suite, Codename or directory."""
    t = pool.track_of_point(row.point)
    return suite in {row.suite, t.suite, t.codename, t.name, t.name.rsplit('/', 1)[-1] if t.name else None}


def delete_plan(pool, cfg, repo, when, suite=None, before=False) -> list[DeleteItem]:
    """The kept points of `repo` dated `when` (or before it, with `before`),
    optionally in one suite, each with what it frees and why it is protected."""
    from mmw.core.points import unsent_points
    matches = _matches(when, before)
    rows = [r for r in prepare(cfg, repos=[repo]) if r.held and matches(r.date)]
    if suite is not None:
        rows = [r for r in rows if _in_suite(pool, r, suite)]
    unsent = {n.name: unsent_points(pool, n) for n in cfg.networks if n.history}
    return [DeleteItem(r.repo, r.suite, r.date, r.point, r.frees, _why_kept(r, unsent)) for r in rows]


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


# ── what changed between two points ──────────────────────────────────────────

def _pick(rows, when: str):
    """The newest of `rows` dated `when` (that day, or that exact stamp)."""
    matches = _matches(when, False)
    return max((r for r in rows if matches(r.date)), key=lambda r: (r.date, r.ident), default=None)


def point_packages(pool, point: int) -> dict:
    from mmw.core.point_diff import packages_of
    from mmw.core.points import _reader
    files = pool.point_files(point)
    return packages_of(files, _reader(pool, files))


def diff_plan(cfg, repo, a, b=None, suite=None) -> list:
    """Per suite of `repo` with a kept point dated `a`: what changed from it
    to the point dated `b` — by default the next kept point after it."""
    from mmw.core.point_diff import SuiteDiff, diff
    from mmw.core.repos import repository_label
    rows = [r for r in prepare(cfg, repos=[repo]) if r.held]
    if not rows:
        raise LookupError(f'{repo} has no kept points — see mmw history')
    out, last = [], []
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        if suite is not None:
            rows = [r for r in rows if _in_suite(pool, r, suite)]
        for name in sorted({r.suite for r in rows}):
            mine = sorted((r for r in rows if r.suite == name), key=lambda r: (r.date, r.ident))
            old = _pick(mine, a)
            if old is None:
                continue
            new = _pick(mine, b) if b is not None else next(iter(mine[mine.index(old) + 1:]), None)
            if new is None:
                last.append(name)
                continue
            out.append(SuiteDiff(repository_label(old.repo, cfg), name, old.date, new.date,
                                 diff(point_packages(pool, old.point), point_packages(pool, new.point))))
    if not out:
        if last and b is None:
            raise LookupError(f'{a} is the newest kept point of {", ".join(last)} — nothing after it to compare')
        raise LookupError(f'no kept point of {repo} is dated {a}' + (f' and {b}' if b else '')
                          + (f' in {suite}' if suite else '') + ' — see mmw history')
    return out


def run_diff(cfg_dir, repo, a, b=None, *, suite=None) -> int:
    import sqlite3
    from mmw.core.point_diff import describe as changes_text
    from mmw.commands.common import eprint, load_config_or_none
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1
    try:
        print(changes_text(diff_plan(cfg, repo, a, b, suite)))
    except (ValueError, LookupError, OSError, sqlite3.Error) as exc:
        eprint(str(exc))
        return 1
    return 0


# ── when a package changed ───────────────────────────────────────────────────

def package_plan(cfg, name: str, *, network=None, repos=None, since=None) -> list:
    """Each kept point where `name`'s version changed, per repository and
    suite, oldest first: the version it arrived with, each new one, and the
    point it left (version None)."""
    from mmw.core.point_diff import PackageEvent, package_events
    from mmw.core.repos import repository_label
    rows = [r for r in prepare(cfg, network=network, repos=repos, since=since) if r.held and not r.legacy]
    out = []
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        for repo, suite in sorted({(r.repo, r.suite) for r in rows},
                                  key=lambda k: (repository_label(k[0], cfg), k[0], k[1])):
            kept = sorted((r for r in rows if (r.repo, r.suite) == (repo, suite)), key=lambda r: (r.date, r.ident))
            for r, version in package_events(name, kept, lambda r: point_packages(pool, r.point)):
                out.append(PackageEvent(repository_label(repo, cfg), suite, r.date, version, r.point))
    if not out:
        raise LookupError(f"{name} isn't in any kept point")
    return out


def run_package(cfg_dir, name: str, *, network=None, repos=None, since=None, as_json_output=False) -> int:
    import sqlite3
    from mmw.core.point_diff import describe_package, events_to_json
    from mmw.commands.common import eprint, load_config_or_none
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1
    try:
        events = package_plan(cfg, name, network=network, repos=repos, since=since)
    except (ValueError, LookupError, OSError, sqlite3.Error) as exc:
        eprint(str(exc))
        return 1
    print(json.dumps(events_to_json(events), indent=2) if as_json_output else describe_package(name, events))
    return 0
