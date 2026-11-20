"""Automatic registration through CLI, TUI, and actual installed launchers."""
import asyncio
import os
from pathlib import Path
import subprocess

import pytest
from typer.testing import CliRunner

from mmw import cli, completion_setup as setup
from waiting import until


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('XDG_DATA_HOME', raising=False)
    return tmp_path


def script(home):
    return home / '.local/share/bash-completion/completions/mmw'


def test_preserves_profile_bytes_permissions_and_symlink_and_refreshes_script(home):
    profile = home / 'my bashrc'
    original = b'export KEEP=yes\n# non-UTF8: \xff'
    profile.write_bytes(original)
    profile.chmod(0o600)
    rc = home / '.bashrc'
    rc.symlink_to(profile)
    setup.install()
    assert rc.is_symlink() and profile.stat().st_mode & 0o777 == 0o600
    assert profile.read_bytes().startswith(original)
    assert profile.read_bytes().count(setup._MARKER) == 1
    first = script(home).stat().st_mtime_ns
    setup.install()
    assert script(home).stat().st_mtime_ns == first
    assert profile.read_bytes().count(setup._MARKER) == 1
    script(home).write_text('outdated script')
    setup.install()
    assert 'complete -F _mmw_completion mmw' in script(home).read_text()


def test_xdg_prefix_and_bash_startup_without_bash_completion_package(home, monkeypatch):
    data = home / "data with 'quotes"
    monkeypatch.setenv('XDG_DATA_HOME', str(data))
    prefix = home / 'custom prefix'
    setup.install(prefix)
    target = data / 'bash-completion/completions/mmw'
    assert target.read_bytes() == (prefix / 'share/bash-completion/completions/mmw').read_bytes()
    result = subprocess.run(['bash', '--noprofile', '--rcfile', str(home / '.bashrc'),
                             '-ic', 'complete -p mmw'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'complete -F _mmw_completion mmw' in result.stdout


@pytest.mark.parametrize('args', [['--version'], ['--help'], ['site', '--help']])
def test_cli_first_launch_is_silent_including_eager_options(home, monkeypatch, args):
    monkeypatch.setattr(setup, 'interactive', lambda: True)
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert script(home).is_file()
    if args == ['--version']:
        assert result.stdout == f'mmw {cli._version()}\n'
    assert not result.stderr


def test_tui_first_launch_has_same_setup(home, monkeypatch):
    monkeypatch.setattr(setup, 'interactive', lambda: True)
    checked = []

    def run(app):
        async def check():
            async with app.run_test(size=(120, 40)) as pilot:
                await until(pilot, lambda: app.screen.query('#mirror-dir'))
                assert script(home).is_file()
                checked.append(True)
        asyncio.run(check())
    monkeypatch.setattr(cli, '_run_tui', run)
    result = CliRunner().invoke(cli.app, ['--config', str(home), 'init'])
    assert result.exit_code == 0, result.output
    assert checked == [True]


def test_noninteractive_launch_and_tab_do_not_write(home, monkeypatch):
    result = CliRunner().invoke(cli.app, ['--version'])
    assert result.exit_code == 0 and not (home / '.bashrc').exists()
    monkeypatch.setattr(setup, 'interactive', lambda: True)
    result = CliRunner().invoke(cli.app, [], prog_name='mmw', env={
        '_MMW_COMPLETE': 'complete_bash', 'COMP_WORDS': 'mmw bu', 'COMP_CWORD': '1'})
    assert result.exit_code == 0, result.output
    assert 'bundle' in result.stdout and not (home / '.bashrc').exists()


def test_unwritable_home_never_breaks_launch(home, monkeypatch):
    monkeypatch.setattr(setup, 'interactive', lambda: True)
    monkeypatch.setattr(setup.ops, 'open_file', lambda *a, **kw: (_ for _ in ()).throw(PermissionError()))
    result = CliRunner().invoke(cli.app, ['--version'])
    assert result.exit_code == 0 and not result.stderr


def test_real_entry_point_installs_on_a_terminal(home):
    import pty
    master, slave = pty.openpty()
    try:
        executable = Path(__file__).resolve().parents[1] / '.venv/bin/mmw'
        result = subprocess.run([str(executable), '--version'], stdin=slave, stdout=slave,
                                stderr=slave, env=os.environ, timeout=20)
        assert result.returncode == 0
        assert script(home).is_file()
    finally:
        os.close(slave)
        os.close(master)


def test_concurrent_launches_do_not_duplicate_or_truncate_setup(home):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: setup.install(), range(16)))
    assert (home / '.bashrc').read_bytes().count(setup._MARKER) == 1
    subprocess.run(['bash', '-n', str(script(home))], check=True)
