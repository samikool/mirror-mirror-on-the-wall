"""Exercise the shell protocol, with isolated config and no command execution."""
import json
import shlex
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mmw.cli import app
from mmw.core import config


def complete(*words):
    env = {'_MMW_COMPLETE': 'complete_bash', 'COMP_WORDS': shlex.join(['mmw', *words]),
           'COMP_CWORD': str(len(words)), 'MMW_NO_UPDATE_CHECK': '1'}
    result = CliRunner().invoke(app, [], prog_name='mmw', env=env)
    assert result.exit_code == 0, (result.output, result.exception)
    assert not result.stderr
    return [line for line in result.stdout.splitlines() if line]


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / 'pool', update_check=False), sources=[
        config.SourceConfig(url='https://apt.test/ubuntu', name='Ubuntu', suites=['noble'],
                            components=['main', 'universe'], arches=['amd64']),
        config.SourceConfig(url='https://apt.test/debian', name='Debian', suites=['stable'],
                            components=['main'], arches=['arm64']),
    ], networks=[config.NetworkConfig(name='work', select=[config.NetworkSelect(
        source='https://apt.test/ubuntu')]), config.NetworkConfig(name='lab')])
    config.save(cfg, tmp_path)
    return cfg


def test_commands_flags_and_builtin_shells_without_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert complete('bu') == ['bundle']
    assert '--network' in complete('bundle', '--')
    assert complete('bundle', '--network', '') == []
    assert complete('add', '') == ['local', 'pypi', 'vscode']


def test_networks_aliases_selection_and_repeatables(configured):
    assert complete('bundle', '--network', '') == ['lab', 'work']
    assert complete('stage', '-n', 'work', '--repo', '') == ['Ubuntu', 'https://apt.test/ubuntu']
    assert complete('bundle', '--repo', 'Ubuntu', '--repo', '') == ['Debian', 'https://apt.test/debian']
    assert complete('bundle', '--repo', 'https://apt.test/debian', '--repo', '') == ['Ubuntu', 'https://apt.test/ubuntu']
    assert complete('bundle', '--network', 'missing', '--repo', '') == []


def test_alternate_config_does_not_leak_between_invocations(configured, tmp_path):
    alt = tmp_path / 'other config'
    alt.mkdir()
    config.save(configured.model_copy(update={'networks': [config.NetworkConfig(name='elsewhere')]}), alt)
    assert complete('-c', str(alt), 'bundle', '--network', '') == ['elsewhere']
    assert complete('bundle', '--network', '') == ['lab', 'work']


def test_fixed_vocabularies_and_dimensions(configured):
    assert complete('add', 'pypi', '--platform', '') == ['linux-glibc', 'linux-musl', 'macos', 'windows']
    assert 'linux-x64' in complete('add', 'vscode', '--platform', '')
    assert complete('add', 'vscode', '--kind', 'desktop', '--kind', '') == ['extensions', 'server']
    assert '3.13' in complete('add', 'pypi', '--python', '3.')
    assert complete('packages', 'add', '--into', 'Ubuntu', '--suite', '') == ['noble']
    assert complete('packages', 'add', '--network', 'work', '--into', '') == ['Ubuntu']
    assert complete('packages', 'add', '--into', 'Ubuntu', '--component', '') == ['main', 'universe']
    assert complete('sources-file', '--format', '') == ['deb822', 'list']
    assert complete('packages', 'add', '--on-clash', '') == ['drop', 'keep']


def test_keys_read_registry_without_running_gpg(configured, monkeypatch):
    keys = configured.settings.mirror_dir / '.signing'
    keys.mkdir(parents=True)
    (keys / 'names.json').write_text(json.dumps({'tools': 'fingerprint', 'other': 'another'}))
    import subprocess
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('completion ran a subprocess'))
    assert complete('signing-key', 'export', '') == ['other', 'tools']
    assert complete('packages', 'add', '--resign', 't') == ['tools']
    assert complete('signing-key', 'generate', '') == []


def test_bad_config_and_control_characters_are_quiet(configured, tmp_path):
    keys = configured.settings.mirror_dir / '.signing'
    keys.mkdir(parents=True)
    (keys / 'names.json').write_text(json.dumps({'bad\nvalue': 'x', 'ok': 'y'}))
    assert complete('signing-key', 'export', '') == ['ok']
    (tmp_path / 'mmw.toml').write_text('not valid toml')
    assert complete('bundle', '--network', '') == []
    assert complete('add', 'pypi', '--platform', 'win') == ['windows']


def test_paths_for_string_arguments_preserve_spaces(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'bundle with spaces.tar').touch()
    (tmp_path / 'stage folder').mkdir()
    assert complete('site', 'apply', 'bu') == ['bundle with spaces.tar']
    assert complete('site', 'apply', 'st') == ['stage folder/']
    assert complete('add', 'local', '--folder', 'st') == ['stage folder/']


@pytest.fixture
def local_site(tmp_path, monkeypatch):
    from mmw.site.home import init_root
    from mmw.site import recents
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    root = tmp_path / 'site root'
    home = init_root(root, name='primary', set_id='set1', network='work')
    from mmw.site.db import SiteDB
    with SiteDB(home):
        pass
    with sqlite3.connect(home / 'site.db') as db:
        db.execute("INSERT INTO repos(repo, label) VALUES ('https://apt.test/debian', 'Debian')")
        db.execute("INSERT INTO tracks(id,repo,name,suite,codename) VALUES (1,'https://apt.test/debian','apt.test/debian/dists/stable','stable','trixie')")
        db.execute("INSERT INTO points(id,track,ident,date,stamp,snap_label) VALUES (1,1,?,?,?,?)",
                   ('release:' + 'a' * 64, '20261101T000000Z', '20261102T000000Z', 'work day'))
        db.execute("INSERT INTO exports(target,repo,ident,stamp) VALUES ('branch', 'https://apt.test/debian', 'a', 'now')")
        db.execute("INSERT INTO meta VALUES ('local:onsite', '/some/instance')")
    recents.remember(str(root))
    return root, home


def test_site_roots_repos_tracks_points_keys_and_export_targets(local_site):
    root, home = local_site
    keys = home / '.signing'
    keys.mkdir()
    (keys / 'names.json').write_text('{"site-key":"fingerprint"}')
    assert str(root) in complete('site', 'status', '--root', '')
    assert complete('site', 'serve', '') == ['Debian', 'https://apt.test/debian']
    assert complete('site', 'serve', 'Debian', '--track', '') == ['apt.test/debian/dists/stable', 'stable', 'trixie']
    assert complete('site', 'serve', 'Debian', '--track', 'stable', '') == [
        '2026-11-01', '20261101T000000Z', '20261102T000000Z', 'a' * 64, 'work day']
    assert complete('site', 'serve', 'Debian', '--track', 'absent', '') == []
    assert complete('site', 'bundle', '--to', '') == ['branch']
    assert complete('site', 'local', 'remove', '') == ['onsite']
    assert complete('site', 'move', 'Debian', '') == ['primary']
    assert complete('site', 'signing-key', 'remove', '') == ['site-key']
    assert complete('site', 'combine', 'Debian', '--key', '') == ['site-key']


def test_completion_never_upgrades_site_or_connects_ssh(local_site, monkeypatch):
    root, home = local_site
    from mmw.site import recents, ssh
    from mmw.site.db import SiteDB
    recents.remember('offline:/site')
    before = {p: p.read_bytes() for p in home.iterdir() if p.is_file()}
    monkeypatch.setattr(ssh, 'run', lambda *a, **kw: pytest.fail('completion connected SSH'))
    monkeypatch.setattr(SiteDB, '__init__', lambda *a, **kw: pytest.fail('completion opened writable SiteDB'))
    assert 'Debian' in complete('site', 'hold', '--root', str(root), '')
    assert complete('site', 'hold', '--root', 'offline:/site', '') == []
    assert 'offline:/site' in complete('site', 'status', '--root', 'off')
    assert {p: p.read_bytes() for p in home.iterdir() if p.is_file()} == before


def test_dates_use_repo_points_before_network_creation_and_selected_arch(tmp_path, monkeypatch):
    from test_repository_points import apt_repository, change_release
    from mmw.core import points
    from mmw.core.pool import Pool
    cfg, md, units = apt_repository(tmp_path)
    with Pool(md) as pool:
        change_release(pool, units[1], b'Suite: stable\nDate: Mon, 02 Nov 2026 00:00:00 UTC\n')
    points.record_points_after_sync(cfg, md, {units[0].source}, say=lambda _: None)
    net = config.NetworkConfig(name='new', nginx=False, certs=False, select=[config.NetworkSelect(
        source=units[0].source, arches=['amd64'])])
    config.save(cfg.model_copy(update={'networks': [net]}), tmp_path)
    monkeypatch.chdir(tmp_path)
    before = (md / '.pool/mmw.db').read_bytes()
    monkeypatch.setattr(Pool, 'point_files', lambda *a: pytest.fail('completion walked file membership'))
    assert complete('bundle', '-n', 'new', '--until', '') == ['2026-11-01', '20261101T000000Z']
    assert (md / '.pool/mmw.db').read_bytes() == before


def test_bash_transport_preserves_quoting_and_word_breaks(configured, tmp_path, monkeypatch):
    import os
    import subprocess
    # Run the generated function in real Bash; no user startup files involved.
    import typer.completion
    monkeypatch.setattr(typer.completion, '_get_shell_name', lambda: 'bash')
    script = CliRunner().invoke(app, ['--show-completion'], prog_name='mmw')
    assert script.exit_code == 0, script.output
    source = tmp_path / 'completion.bash'
    source.write_text(script.stdout)
    executable = Path(__file__).resolve().parents[1] / '.venv/bin/mmw'
    def run(line, words, index):
        harness = '\n'.join([
            'source ' + shlex.quote(str(source)),
            'COMP_LINE=' + shlex.quote(line),
            'COMP_POINT=${#COMP_LINE}',
            'COMP_WORDS=(' + shlex.join(words) + ')',
            'COMP_CWORD=' + str(index),
            '_mmw_completion ' + shlex.quote(str(executable)),
            'printf "%s\\n" "${COMPREPLY[@]}"',
        ])
        result = subprocess.run(['bash', '--noprofile', '--norc', '-c', harness], capture_output=True,
                                text=True, check=True, env={**os.environ, 'MMW_NO_UPDATE_CHECK': '1'})
        assert not result.stderr
        return [s for s in result.stdout.splitlines() if s]
    assert run('mmw --config "' + str(tmp_path) + '" bundle --network w',
               ['mmw', '--config', str(tmp_path), 'bundle', '--network', 'w'], 5) == ['work']
    # Readline splits an unquoted URL at ':' and replaces only //apt.test/u.
    assert run('mmw bundle --repo https://apt.test/u',
               ['mmw', 'bundle', '--repo', 'https', ':', '//apt.test/u'], 5) == ['//apt.test/ubuntu']
    # Quoting the URL prevents that split; keep the whole candidate.
    assert run('mmw bundle --repo "https://apt.test/u',
               ['mmw', 'bundle', '--repo', 'https://apt.test/u'], 3) == ['https://apt.test/ubuntu']
    assert run('mmw bundle --network=w',
               ['mmw', 'bundle', '--network', '=', 'w'], 4) == ['work']
    folder = tmp_path / 'folder with spaces'
    folder.mkdir()
    (folder / 'bundle.tar').touch()
    assert run('mmw site apply "folder with spaces/bu',
               ['mmw', 'site', 'apply', 'folder with spaces/bu'], 3) == ['folder with spaces/bundle.tar']


def test_deploy_uses_selected_bundle_header_only(tmp_path, monkeypatch):
    import io
    import tarfile
    from mmw.core.publisher.manifest import RECORD_PATH
    monkeypatch.chdir(tmp_path)
    header = json.dumps({'format': 3, 'network': 'elsewhere', 'created': '20261101T000000Z',
                         'points': {'https://apt.test/ubuntu': {'label': 'Ubuntu'}}}).encode() + b'\n'
    with tarfile.open('bundle.tar', 'w') as tar:
        member = tarfile.TarInfo(RECORD_PATH)
        member.size = len(header)
        tar.addfile(member, io.BytesIO(header))
    assert complete('site', 'apply', 'bundle.tar', '--deploy', '') == ['Ubuntu', 'https://apt.test/ubuntu']
    assert complete('site', 'apply', '-', '--deploy', '') == []


def test_site_local_networks_come_from_selected_instance(configured, local_site, tmp_path):
    other = tmp_path / 'instance'
    other.mkdir()
    config.save(configured.model_copy(update={'networks': [config.NetworkConfig(name='other')]}), other)
    assert complete('site', 'local', 'add', str(other), '--network', '') == ['other']


def test_install_and_show_emit_same_bash_function(tmp_path, monkeypatch):
    import typer.completion
    from typer import _completion_shared
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(typer.completion, '_get_shell_name', lambda: 'bash')
    monkeypatch.setattr(_completion_shared, '_get_shell_name', lambda: 'bash')
    runner = CliRunner()
    shown = runner.invoke(app, ['--show-completion'], prog_name='mmw')
    installed = runner.invoke(app, ['--install-completion'], prog_name='mmw')
    assert shown.exit_code == installed.exit_code == 0, (shown.output, installed.output)
    script = tmp_path / '.bash_completions/mmw.sh'
    assert script.read_text().strip() == shown.stdout.strip()
    assert str(script) in (tmp_path / '.bashrc').read_text()


def test_ambiguous_labels_use_keys(configured, tmp_path):
    # Source names must be unique case-sensitively, but repo resolution is
    # case-insensitive. Neither ambiguous alias should be suggested.
    sources = [s.model_copy(update={'name': n}) for s, n in zip(configured.sources, ['same', 'SAME'])]
    config.save(configured.model_copy(update={'sources': sources}), tmp_path)
    assert complete('bundle', '--repo', '') == ['https://apt.test/debian', 'https://apt.test/ubuntu']


def test_site_scope_components_and_mapping(local_site, tmp_path):
    root, home = local_site
    from mmw.site import recents
    from mmw.site.home import init_root, load_site, save_settings
    other = tmp_path / 'unrelated'
    other_home = init_root(other, name='other', set_id='different', network='different')
    from mmw.site.db import SiteDB
    with SiteDB(other_home):
        pass
    with sqlite3.connect(other_home / 'site.db') as db:
        db.execute("INSERT INTO repos(repo,label) VALUES ('other', 'Other')")
    recents.remember(str(other))
    assert complete('site', 'hold', '') == []  # needs a set selection
    assert complete('site', 'hold', '--root', str(root), '') == ['Debian', 'https://apt.test/debian']
    with sqlite3.connect(home / 'site.db') as db:
        db.execute("INSERT INTO paths(path) VALUES ('apt.test/debian/dists/stable/main/binary-amd64/Packages')")
    save_settings(home, load_site(home).model_copy(update={'hosts': {'apt.test': 'primary'}}))
    assert complete('site', 'packages', 'add', '--root', str(root), '--repo', 'Debian', '--component', '') == ['main']
    # apply's placement options are different from selecting an opened root.
    recents.forget(str(other))
    assert complete('site', 'apply', '--root', '') == ['*=', 'apt.test=']
    assert complete('site', 'apply', '--root', '*=un') == ['*=unrelated/']


@pytest.mark.parametrize('line, expected', [
    ('mmw add pypi --platform ', (['add', 'pypi', '--platform'], '')),
    ('mmw site open "folder with spaces', (['site', 'open'], 'folder with spaces')),
    ('mmw site open folder\\ with\\ spaces\\ ', (['site', 'open'], 'folder with spaces ')),
    ("mmw site open 'it'\\''s here", (['site', 'open'], "it's here")),
])
def test_bash_partial_quoting(line, expected):
    from mmw.completion_bash import _line_args
    from typer._completion_classes import click_split_arg_string
    assert _line_args(line, click_split_arg_string) == expected


def test_native_path_parameters_also_complete_in_protocol(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'kept.tar').touch()
    (tmp_path / 'config folder').mkdir()
    assert complete('init', '--from-bundle', 'ke') == ['kept.tar']
    assert complete('--config', 'con') == ['config folder/']
    assert complete('installer', '--out', 'ke') == []
    assert complete('bundle', '--out', 'ke') == ['kept.tar']
    assert complete('bundle', '--repo', 'ke') == []


def test_pypi_and_vscode_use_provider_repository_keys(configured, tmp_path):
    cfg = configured.model_copy(update={'sources': [*configured.sources,
        config.PypiSource(provider='pypi'), config.VscodeSource(provider='vscode')]})
    config.save(cfg, tmp_path)
    repos = complete('bundle', '--repo', '')
    assert 'pypi' in repos and 'vscode' in repos
    assert not any('pypi.org' in r or 'visualstudio.com' in r for r in repos)
    assert complete('sync', '--repo', '') == ['Debian', 'Ubuntu', 'https://apt.test/debian', 'https://apt.test/ubuntu']


def test_provider_inputs_reuse_configured_values(configured, tmp_path):
    cfg = configured.model_copy(update={'sources': [*configured.sources,
        config.PypiSource(provider='pypi', requirements=['requests>=2'], glibc='2.28'),
        config.VscodeSource(provider='vscode', extensions=config.ExtensionSelection(ids=['ms-python.python']))]})
    config.save(cfg, tmp_path)
    assert complete('add', 'pypi', '--req', 'req') == ['requests>=2']
    assert complete('add', 'pypi', '--glibc', '') == ['2.28', '2.31']
    assert complete('add', 'vscode', '--id', 'ms-') == ['ms-python.python']


def test_tui_and_shell_offer_the_same_retained_dates(tmp_path, monkeypatch):
    from textual.widgets import Select
    from test_repository_points import apt_repository
    from test_snapshot_tui import _run
    from mmw.core import points
    from mmw.commands.bundle import available_transfer_dates
    from waiting import until
    cfg, md, units = apt_repository(tmp_path)
    points.record_points_after_sync(cfg, md, {units[0].source}, say=lambda _: None)
    net = config.NetworkConfig(name='new', nginx=False, certs=False, bundle_dir=tmp_path / 'usb')
    cfg = cfg.model_copy(update={'networks': [net]})
    config.save(cfg, tmp_path)
    monkeypatch.chdir(tmp_path)
    dates = available_transfer_dates(net, md, cfg=cfg)
    assert set(dates) <= set(complete('bundle', '-n', net.name, '--until', ''))
    async def check(tui, pilot):
        await pilot.press('t')
        await until(pilot, lambda: type(tui.screen).__name__ == 'BundlePickPage'
                    and tui.screen.query('#pick-to') and tui.screen.query_one('#pick-to').is_mounted)
        choices = {value for _, value in tui.screen.query_one('#pick-to', Select)._options}
        assert {f'point:{date}' for date in dates} <= choices
    _run(cfg, tmp_path, check)
