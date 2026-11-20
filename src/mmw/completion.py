"""Context-aware shell suggestions. Never sync, migrate, prompt, or contact a host.

Typer owns the shell protocol and command/flag completion. Callbacks here only
read configuration and retained metadata; failure means no suggestions. In
particular, opening a far-side SiteDB normally upgrades it, so these readers use
SQLite's read-only mode instead.
"""
import contextlib
import inspect
import io
import json
import os
import sqlite3
from pathlib import Path
from typing import get_args

import typer
from typer.models import ParameterInfo

from mmw.core import config, disk_io


def _values(value):
    return list(value) if isinstance(value, (list, tuple)) else [value] if value else []


def _cfg(ctx):
    directory = ctx.find_root().params.get('config_dir') or Path('.')
    if ctx.command_path.endswith('site local add'):
        directory = ctx.params.get('path') or directory
    # Old configurations can emit a retirement notice. Shell protocols must
    # contain candidates only, including when the user has an old config.
    with contextlib.redirect_stderr(io.StringIO()):
        return config.load(Path(directory))


@contextlib.contextmanager
def _database(path):
    conn = sqlite3.connect(Path(path).absolute().as_uri() + '?mode=ro', uri=True, timeout=0.05)
    try:
        conn.execute('PRAGMA query_only = ON')
        yield conn
    finally:
        conn.close()


def _paths(incomplete, *, directories=False):
    """One directory only; do not recursively walk a pool while pressing Tab."""
    if incomplete == '~':
        yield '~/'
        return
    expanded = os.path.expanduser(incomplete)
    directory, prefix = os.path.split(expanded)
    shown_dir = incomplete[:len(incomplete) - len(prefix)] if prefix else incomplete
    for path in disk_io.ops.iterdir(Path(directory or '.')):
        if not path.name.startswith(prefix) or (path.name.startswith('.') and not prefix.startswith('.')):
            continue
        is_dir = disk_io.ops.is_dir(path)
        if not directories or is_dir:
            yield shown_dir + path.name + ('/' if is_dir else '')


def _repo_choices(repos, used=()):
    """Offer usable aliases, avoiding ambiguous labels and already chosen repos."""
    from mmw.core.repos import resolve_repo_refs
    selected = set()
    for ref in used:
        with contextlib.suppress(LookupError):
            selected.update(resolve_repo_refs([ref], repos))
    for key, label in repos.items():
        if key in selected:
            continue
        yield key
        if sum(v.lower() == label.lower() for v in repos.values()) == 1:
            yield label


def _units(cfg, ctx):
    from mmw.core.resolve import available_mirrors, network_mirrors
    network = ctx.params.get('network')
    if network:
        net = next(n for n in cfg.networks if n.name == network)
        return network_mirrors(net, cfg, cfg.settings.mirror_dir)
    return available_mirrors(cfg, cfg.settings.mirror_dir)


def _connected_repos(cfg, ctx):
    from mmw.core.repos import repository_key, repository_label
    keys = {repository_key(u) for u in _units(cfg, ctx)}
    # An unsynced, open-ended source still has an identity, even if there are
    # no concrete units yet. Network filters must still be respected.
    if not ctx.params.get('network'):
        from mmw.providers import get_provider
        keys.update(s.provider if get_provider(s.provider).repository_scope == 'provider' else s.identity
                    for s in cfg.sources)
    command = ctx.command.name
    if command == 'sync':
        keys &= {s.identity for s in cfg.sources if s.provider == 'apt'}
    elif command == 'keep':
        keys &= {s.identity for s in cfg.sources if isinstance(s, config.SourceConfig)}
    return {key: repository_label(key, cfg) for key in keys}


def _sources(cfg, ctx):
    """Package commands take source names, not provider repository aliases."""
    units = _units(cfg, ctx)
    identities = {u.source for u in units}
    network = ctx.params.get('network')
    for source in cfg.sources:
        if network and source.identity not in identities:
            continue
        if source.provider not in ('apt', 'apt-local'):
            continue
        if ctx.command.name == 'signing' and source.provider != 'apt':
            continue
        if ctx.command.name == 'sources-file' and not network and source.provider != 'apt-local':
            continue
        yield source


def _selected_units(cfg, ctx):
    from mmw.core.repos import repository_key, resolve_repo_refs
    units = _units(cfg, ctx)
    refs = _values(ctx.params.get('repo'))
    if refs:
        keys = resolve_repo_refs(refs, _connected_repos(cfg, ctx))
        units = [u for u in units if repository_key(u) in keys]
    into = ctx.params.get('into') or ctx.params.get('name')
    if into:
        sources = [s for s in cfg.sources if s.name == into or s.identity == into]
        units = [u for u in units if any(s.identity == u.source for s in sources)]
    suite = ctx.params.get('suite')
    if suite:
        units = [u for u in units if u.suite == suite]
    return units


def _connected_dates(cfg, ctx):
    """Read just Release metadata, not thousands of package rows per point."""
    from mmw.core.pool import Pool
    from mmw.core.repository_points import _project, wire_draft
    from mmw.core.repos import repository_key
    from mmw.site.versionref import day
    units = _selected_units(cfg, ctx)
    settings = json.dumps({'mirrors': [u.legacy_dict() for u in units],
                           'repos': {repository_key(u): [u.mirror_base] for u in units}})
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        # Providers whose catalogs are built per network still retain legacy
        # points. Read those through their existing history adapter.
        wanted = set(_connected_repos(cfg, ctx))
        refs = _values(ctx.params.get('repo'))
        if refs:
            from mmw.core.repos import resolve_repo_refs
            wanted = set(resolve_repo_refs(refs, _connected_repos(cfg, ctx)))
        for net in cfg.networks:
            if ctx.params.get('network') and net.name != ctx.params['network']:
                continue
            for point in pool.network_points(net.name):
                if point.legacy and pool.track_of_point(point.id).repo in wanted:
                    yield day(point.date)
                    yield point.date
        for repo in {repository_key(u) for u in units}:
            mine = [u for u in units if repository_key(u) == repo]
            with pool._read() as conn:
                unit_ids = [row[0] for u in mine for row in conn.execute(
                    'SELECT id FROM units WHERE repo = ? AND dims = ?',
                    (u.source, json.dumps(u.dims, sort_keys=True, separators=(',', ':'))))]
            for point in pool.repository_points(repo):
                track = pool.track_of_point(point.id)
                if track.name and not any(track.name in (u.mirror_base, u.mirror_base + '/dists/' + (u.suite or ''))
                                          for u in mine):
                    continue
                date = point.date
                if not point.legacy and track.name:
                    rows = []
                    with pool._read() as conn:
                        for unit_id in [0, *unit_ids]:
                            for name in ('Release', 'InRelease', 'Release.gpg'):
                                rows.extend(conn.execute(
                                    'SELECT p.path, b.checksum, f.kind, f.hashed, f.unit = 0 '
                                    'FROM point_unit_files f JOIN snapshot_paths p ON p.id = f.path '
                                    'JOIN snapshot_blobs b ON b.id = f.blob '
                                    'WHERE f.point = ? AND f.unit = ? AND f.path = '
                                    '(SELECT id FROM snapshot_paths WHERE path = ?)',
                                    (point.id, unit_id, track.name + '/' + name)))
                    if not rows:
                        continue
                    try:
                        date = wire_draft(pool, point.id, _project(rows), settings, point.stamp).date
                    except (OSError, ValueError, StopIteration):
                        continue  # Missing pool metadata is not a usable endpoint.
                yield day(date)
                yield date


def _keys(base):
    names = json.loads(disk_io.ops.read_text(base / '.signing' / 'names.json'))
    return names.keys() if isinstance(names, dict) else ()


def _site_roots(ctx):
    from mmw.site import recents
    from mmw.site.home import read_root
    from mmw.site.ssh import parse_location
    explicit = ctx.params.get('root')
    if isinstance(explicit, (list, tuple)):
        explicit = None  # apply's HOST=PATH placements, not a selected site
    locations = _values(explicit) + recents.known()
    if not explicit:
        locations.append(str(Path.cwd()))
    roots = []
    seen = set()
    for location in locations:
        if parse_location(str(location))[0] == 'ssh':
            continue
        with contextlib.suppress(OSError, ValueError):
            root = read_root(str(location))
            if root is not None and root.path not in seen:
                roots.append(root)
                seen.add(root.path)
    if explicit:
        anchor = next((r for r in roots if r.location == str(explicit)), None)
        if anchor is None:
            cached = recents.last_seen(str(explicit)) or {}
            return [r for r in roots if cached.get('set_id') and r.set_id == cached['set_id']]
        roots = [r for r in roots if r.path == anchor.path or (anchor.set_id and r.set_id == anchor.set_id)]
    elif len({r.set_id or str(r.path) for r in roots}) > 1:
        return []  # The command also needs --root to disambiguate sets.
    return roots


def _site_repos(conn):
    moving = {r[0].removeprefix('moving_in:') for r in conn.execute(
        "SELECT key FROM meta WHERE key LIKE 'moving_in:%'")}
    return {key: label for key, label in conn.execute('SELECT repo, label FROM repos') if key not in moving}


def _site_values(ctx, kind):
    from mmw.core.repos import resolve_repo_refs
    from mmw.site.home import load_site
    from mmw.site.layers import site_layer_dir
    from mmw.site.versionref import day
    for root in _site_roots(ctx):
        try:
            if kind == 'keys':
                yield from _keys(root.home)
                continue
            if kind in ('members', 'hosts'):
                settings = load_site(root.home)
                yield from settings.members if kind == 'members' else settings.hosts
                continue
            with _database(root.home / 'site.db') as conn:
                repos = _site_repos(conn)
                if kind == 'repos':
                    yield from _repo_choices(repos, _values(ctx.params.get('repo')) + _values(ctx.params.get('forward')))
                    continue
                if kind == 'targets':
                    yield from (r[0] for r in conn.execute('SELECT DISTINCT target FROM exports'))
                    continue
                if kind == 'locals':
                    yield from (r[0][6:] for r in conn.execute("SELECT key FROM meta WHERE key LIKE 'local:%' AND value != ''"))
                    continue
                if kind == 'bundles':
                    yield from (r[0] for r in conn.execute('SELECT created FROM applied'))
                    yield from (r[0] for r in conn.execute('SELECT DISTINCT snap_label FROM points WHERE snap_label IS NOT NULL'))
                    continue
                refs = _values(ctx.params.get('repo'))
                keys = resolve_repo_refs(refs, repos) if refs else list(repos)
                for key in keys:
                    if kind == 'files':
                        folder = site_layer_dir(root.home, key)
                        yield from (p.name for p in disk_io.ops.glob(folder, '*/*/*.deb'))
                        continue
                    for track_id, name, suite, codename in conn.execute(
                            'SELECT id, name, suite, codename FROM tracks WHERE repo = ?', (key,)):
                        if kind == 'tracks':
                            yield from (name, suite, codename, name.rsplit('/', 1)[-1])
                            continue
                        if kind == 'components':
                            selected = ctx.params.get('suite')
                            if selected and selected not in (name, suite, codename, name.rsplit('/', 1)[-1]):
                                continue
                            prefix = name + '/'
                            for (path,) in conn.execute('SELECT path FROM paths WHERE path >= ? AND path < ?',
                                                        (prefix, name + '0')):
                                parts = path[len(prefix):].split('/')
                                if len(parts) >= 3 and parts[1].startswith(('binary-', 'source', 'debian-installer')):
                                    yield parts[0]
                            continue
                        track = ctx.params.get('track')
                        if track and track not in (name, suite, codename, name.rsplit('/', 1)[-1]):
                            continue
                        for ident, date, stamp, label in conn.execute(
                                'SELECT ident, date, stamp, snap_label FROM points WHERE track = ? AND held = 1', (track_id,)):
                            yield day(date)
                            yield date
                            if kind != 'dates':
                                yield from (ident.split(':', 1)[-1], stamp, label)
        except (OSError, ValueError, LookupError, sqlite3.Error):
            continue  # An unplugged root must not hide other local roots.


def _candidates(ctx, kind, incomplete):
    if kind == 'deploy':
        from mmw.core.publisher.kept import read_manifest_header
        from mmw.core.publisher.manifest import RECORD_PATH
        src = ctx.params.get('src')
        if src and src != '-':
            path = Path(src)
            header = read_manifest_header(path / RECORD_PATH if disk_io.ops.is_dir(path) else path)
            repos = {key: data.get('label', key) for key, data in (header or {}).get('points', {}).items()}
            yield from _repo_choices(repos, _values(ctx.params.get('deploy')))
        return
    if kind in ('paths', 'directories'):
        yield from _paths(incomplete, directories=kind == 'directories')
        return
    if kind == 'roots':
        from mmw.site import recents
        yield from recents.known()
        yield from _paths(incomplete, directories=True)
        return
    if kind == 'mapping':
        if '=' in incomplete:
            host, path = incomplete.split('=', 1)
            yield from (host + '=' + p for p in _paths(path, directories=True))
        else:
            yield '*='
            yield from (host + '=' for host in _site_values(ctx, 'hosts'))
        return
    if kind.startswith('site:'):
        yield from _site_values(ctx, kind[5:])
        return
    fixed = {'formats': ('deb822', 'list'), 'clashes': ('keep', 'drop'),
             'retention': ('all',), 'providers': ('vscode', 'pypi', 'local')}
    if kind in fixed:
        yield from fixed[kind]
        if kind == 'retention' and ctx.command.name == 'keep' and ctx.params.get('repo'):
            yield 'default'
        return
    if kind in ('kind', 'quality', 'platform', 'python', 'arch'):
        from mmw.providers.pypi import targets
        from mmw.providers.vscode import platforms
        choices = {'kind': platforms.KINDS, 'quality': platforms.QUALITIES,
                   'python': targets.PYTHONS,
                   'platform': targets.PLATFORMS if ctx.params.get('what') == 'pypi' else platforms.TARGET_PLATFORMS,
                   'arch': targets.ARCHES}
        yield from choices[kind]
        if kind != 'arch':
            return
    cfg = _cfg(ctx)
    if kind == 'networks':
        yield from (n.name for n in cfg.networks)
    elif kind == 'repos':
        yield from _repo_choices(_connected_repos(cfg, ctx), _values(ctx.params.get('repo')))
    elif kind == 'sources':
        yield from (s.name for s in _sources(cfg, ctx))
    elif kind in ('suite', 'component', 'arch'):
        for unit in _selected_units(cfg, ctx):
            yield getattr(unit, {'suite': 'suite', 'component': 'component', 'arch': 'arch'}[kind])
    elif kind == 'dates':
        yield from _connected_dates(cfg, ctx)
    elif kind == 'keys':
        yield from _keys(cfg.settings.mirror_dir)
    elif kind == 'ext_ids':
        for source in cfg.sources:
            if source.provider == 'vscode':
                yield from source.extensions.ids
    elif kind == 'requirements':
        for source in cfg.sources:
            if source.provider == 'pypi':
                yield from source.requirement_lines()
    elif kind in ('glibc', 'musl', 'macos'):
        from mmw.providers.pypi.targets import Floors
        yield getattr(Floors(), kind)
        for source in cfg.sources:
            if source.provider == 'pypi':
                yield getattr(source, kind)
    elif kind == 'files':
        from mmw.core.layers import layer_dir
        into = ctx.params.get('into')
        for source in _sources(cfg, ctx):
            if into and into not in (source.name, source.identity):
                continue
            if source.provider == 'apt-local' and not ctx.params.get('network'):
                yield from (p.name for p in disk_io.ops.rglob(source.folder_path(), '*.deb'))
            for network in cfg.networks:
                if ctx.params.get('network') and network.name != ctx.params['network']:
                    continue
                yield from (p.name for p in disk_io.ops.glob(
                    layer_dir(cfg.settings.mirror_dir, network.name, source.name), '*/*/*.deb'))


def _callback(kind, parameter):
    def complete(ctx: typer.Context, incomplete: str):
        # A missing option value makes resilient parsing leave positional
        # arguments in ctx.args (e.g. `add pypi --platform <Tab>`). Recover
        # their raw values without invoking converters or command callbacks.
        remaining = list(ctx.args)
        for param in ctx.command.params:
            if param.param_type_name != 'argument' or ctx.params.get(param.name):
                continue
            if not remaining or remaining[0].startswith('-'):
                break
            count = len(remaining) if param.nargs == -1 else param.nargs
            values, remaining = remaining[:count], remaining[count:]
            ctx.params[param.name] = values if param.nargs != 1 else values[0]
        param = next(p for p in ctx.command.params if p.name == parameter)
        used = _values(ctx.params.get(parameter)) if param.multiple or param.nargs == -1 else []
        out = []
        # Bad config, inaccessible media and old metadata schemas are ordinary
        # during completion. Never let a traceback corrupt the shell response.
        with contextlib.suppress(Exception):
            for value in _candidates(ctx, kind, incomplete):
                if (isinstance(value, str) and value and value.startswith(incomplete)
                        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
                        and value not in used and value not in out):
                    out.append(value)
        return sorted(out)
    return complete


def _kind(path, parameter):
    site = path[0] == 'site'
    command = path[-1]
    if parameter == 'network':
        return 'site:locals' if path == ('site', 'local', 'remove') else 'networks'
    if parameter == 'root' and site:
        return 'mapping' if command == 'apply' else 'roots'
    if parameter == 'path' and path == ('site', 'open'):
        return 'roots'
    if parameter == 'repo' or (site and parameter == 'forward'):
        return 'site:repos' if site else 'repos'
    if site and parameter == 'deploy':
        return 'deploy'
    if parameter in ('since', 'until', 'as_of'):
        return 'site:dates' if site else 'dates'
    if site and parameter in ('version', 'whens'):
        return 'site:points'
    if site and parameter == 'when':
        return 'site:bundles'
    if site and parameter == 'to_root':
        return 'site:members'
    if site and parameter == 'to':
        return 'site:targets'
    if parameter == 'track':
        return 'site:tracks'
    if parameter == 'fmt':
        return 'formats'
    if parameter == 'on_clash':
        return 'clashes'
    if parameter in ('value', 'keep') and command in ('keep', 'settings'):
        return 'retention'
    if parameter == 'name' and 'signing-key' in path and command in ('export', 'remove'):
        return 'site:keys' if site else 'keys'
    if parameter in ('resign', 'key') and (parameter == 'resign' or command in ('add', 'combine')):
        # site adopt --key is a file, left to normal path completion.
        return 'site:keys' if site else 'keys'
    if parameter == 'filename':
        return 'site:files' if site else 'files'
    if parameter == 'into' or (parameter == 'name' and command == 'sources-file'):
        return 'sources'
    if parameter in ('suite', 'component'):
        return ('site:tracks' if parameter == 'suite' else 'site:components') if site else parameter
    if path == ('add',) and parameter in ('what', 'kind', 'quality', 'platform', 'python', 'arch'):
        return 'providers' if parameter == 'what' else parameter
    if path == ('add',) and parameter in ('ext_id', 'req', 'glibc', 'musl', 'macos'):
        return {'ext_id': 'ext_ids', 'req': 'requirements'}.get(parameter, parameter)
    if parameter == 'folder':
        return 'directories'
    if parameter in ('src', 'requirements_file') or (site and command == 'bundle' and parameter == 'out'):
        return 'paths'
    return None


def configure(app, path=()):
    """Attach public Typer callbacks before commands are materialized.

    Parameter names encode the same concepts across the CLI. Central wiring
    keeps shared OptionInfo objects and new commands consistent. Typer's Path
    completion relies on shell fallbacks; explicit callbacks keep paths working
    in every shell without suggesting filenames for repository arguments.
    """
    if not path:
        from mmw.completion_bash import configure_bash
        configure_bash()
    commands = list(app.registered_commands)
    if app.registered_callback:
        commands.append(app.registered_callback)
    for command in commands:
        here = (*path, command.name or command.callback.__name__.replace('_', '-'))
        for parameter in inspect.signature(command.callback).parameters.values():
            kind = _kind(here, parameter.name)
            annotations = [parameter.annotation, *get_args(parameter.annotation)]
            if kind is None and Path in annotations:
                directories = parameter.name in ('config_dir', 'mirror_dir', 'keys_dir', 'root') or (
                    parameter.name == 'out' and here[-1] in ('installer', 'stage', 'certs', 'make'))
                kind = 'directories' if directories else 'paths'
            info = parameter.default
            if kind and isinstance(info, ParameterInfo) and info.autocompletion is None:
                info.autocompletion = _callback(kind, parameter.name)
    for group in app.registered_groups:
        configure(group.typer_instance, (*path, group.name))
