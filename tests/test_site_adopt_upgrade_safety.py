"""First-run upgrade safety: real manifests, readonly Look, and resumable adoption."""
import asyncio
import json
import os
from pathlib import Path

import pytest
from textual.widgets import Input
from typer.testing import CliRunner

from mmw.cli import app
from mmw.core import disk_io
from mmw.site import adopt as A, home as H, recents
from mmw.site.db import SiteDB
from mmw.tui.site.app import SiteApp

from test_site_adopt_apply import far, BASE  # noqa: F401
from test_bundle_snapshots import states, _tree  # noqa: F401
from waiting import until


def cli(dest, *args):
    result = CliRunner().invoke(app, ['site', 'adopt', str(dest), *args])
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    return result


def fingerprint(dest):
    return {str(p.relative_to(dest)): (p.stat().st_ino, p.read_bytes())
            for p in dest.rglob('*') if p.is_file()}


@pytest.fixture(params=['v0.9-zstd', 'v1.2-jsonl'])
def old_far(far, request, monkeypatch, tmp_path):  # noqa: F811
    *_rest, dest = far
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'fresh-state'))
    record = dest / '.mmw/manifest.jsonl'
    lines = record.read_bytes().splitlines()
    header = json.loads(lines[0])
    keep = {'network', 'created', 'previous', 'full', 'file_count'}
    if request.param == 'v1.2-jsonl':
        keep |= {'previous_label', 'created_label'}
    raw = json.dumps({k: v for k, v in header.items() if k in keep}).encode() + b'\n' + b'\n'.join(lines[1:]) + b'\n'
    if request.param == 'v0.9-zstd':
        import zstandard
        raw = zstandard.ZstdCompressor().compress(raw)
    record.write_bytes(raw)
    return dest


def test_old_manifest_look_is_readonly_and_adopt_preserves_served_files(old_far):
    dest = old_far
    before = fingerprint(dest)
    look = cli(dest, '--dry-run')
    assert look.exit_code == 0, look.output
    assert 'Checked ' in look.output
    assert fingerprint(dest) == before
    converted = cli(dest, '--yes')
    assert converted.exit_code == 0, converted.output
    for rel, value in before.items():
        if not rel.startswith('.mmw/'):
            assert ((dest / rel).stat().st_ino, (dest / rel).read_bytes()) == value
    assert not (H.root_home(dest) / 'adoption.json').exists()
    with SiteDB(H.root_home(dest)) as db:
        assert db.repo_states() and db.applied()[0][2] == 'adopted'


@pytest.mark.parametrize('damage', ['changed', 'missing', 'symlink'])
def test_look_and_adopt_reject_damaged_packages_before_initializing(old_far, damage, tmp_path):
    dest = old_far
    deb = dest / next(p for p in _tree(dest) if p.endswith('.deb'))
    body = deb.read_bytes()
    if damage == 'changed':
        deb.write_bytes(b'wrong')
    else:
        deb.unlink()
        if damage == 'symlink':
            outside = tmp_path / 'external.deb'
            outside.write_bytes(body)
            deb.symlink_to(outside)
    before = fingerprint(dest)
    for args in [('--dry-run',), ('--yes',)]:
        result = cli(dest, *args)
        assert result.exit_code == 1, result.output
        assert 'missing' in result.output or 'differs' in result.output
        assert not H.root_home(dest).exists()
        assert fingerprint(dest) == before
    if deb.is_symlink():
        deb.unlink()
    deb.write_bytes(body)
    assert cli(dest, '--dry-run').exit_code == 0
    assert cli(dest, '--yes').exit_code == 0


def test_same_size_change_with_restored_mtime_is_not_hidden_by_look_cache(old_far):
    dest = old_far
    assert cli(dest, '--dry-run').exit_code == 0
    deb = dest / next(p for p in _tree(dest) if p.endswith('.deb'))
    stat = deb.stat()
    deb.write_bytes(b'!' * stat.st_size)
    os.utime(deb, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    adopted = cli(dest, '--yes')
    assert adopted.exit_code == 1 and 'differs' in adopted.output
    assert not H.root_home(dest).exists()


@pytest.mark.parametrize('phase', ['init', 'ingest', 'points', 'render', 'archive', 'archived', 'journal'])
def test_interruption_can_retry_without_resync_or_duplicate_history(old_far, monkeypatch, phase):
    from mmw.site import ops, timeline
    dest = old_far
    before = {p: ((dest / p).stat().st_ino, (dest / p).read_bytes()) for p in _tree(dest)}
    with monkeypatch.context() as patch:
        if phase in ('init', 'ingest', 'points', 'render'):
            owner, name = {'init': (H, 'init_root'), 'ingest': (A, '_ingest'),
                           'points': (timeline, 'import_rows'), 'render': (ops, '_render')}[phase]
            real = getattr(owner, name)
            def interrupted(*args, **kwargs):
                real(*args, **kwargs)
                raise OSError('simulated interrupted adoption')
            patch.setattr(owner, name, interrupted)
        elif phase in ('archive', 'archived'):
            real = disk_io.ops.replace
            def interrupted(src, dst):
                if Path(src).name == 'manifest.jsonl':
                    if phase == 'archived':
                        real(src, dst)
                    raise OSError('simulated interrupted adoption')
                return real(src, dst)
            patch.setattr(disk_io.ops, 'replace', interrupted)
        else:
            real = disk_io.ops.unlink
            def interrupted(path, **kwargs):
                if Path(path).name == 'adoption.json':
                    raise OSError('simulated interrupted adoption')
                return real(path, **kwargs)
            patch.setattr(disk_io.ops, 'unlink', interrupted)
        first = cli(dest, '--yes')
        assert first.exit_code == 1 and 'interrupted adoption' in first.output, first.output
    assert (H.root_home(dest) / 'adoption.json').exists()
    assert not (H.root_home(dest) / 'lock').exists()
    assert H.read_root(str(dest)) is None  # Never expose a partial root as ready.
    A._HASHED.clear()  # Retry must also work in a fresh process.
    second = cli(dest, '--yes')
    assert second.exit_code == 0, second.output
    assert {p: ((dest / p).stat().st_ino, (dest / p).read_bytes()) for p in before} == before
    assert not (H.root_home(dest) / 'adoption.json').exists()
    assert A.find_apply_tree(dest) is None
    with SiteDB(H.root_home(dest)) as db:
        assert len(db.applied()) == 1
        states = db.repo_states()
        assert len(states) == 1 and states[0].serving
        for track in db.tracks(states[0].repo):
            assert len(db.points(track.id)) == 1


def test_stale_lock_is_reported_and_only_explicitly_cleared(old_far):
    from mmw.site.lock import SiteLock
    dest = old_far
    home = H.root_home(dest)
    home.mkdir(parents=True)
    with SiteLock(home):
        assert cli(dest, '--dry-run', '--break-lock').exit_code == 0
        assert (home / 'lock').exists()  # Look must not break an active lock.
        busy = cli(dest, '--yes')
        assert busy.exit_code == 5 and 'site is in use' in busy.output
    (home / 'lock').write_text('gone-host 999999 2000-01-01\n')
    assert cli(dest, '--yes', '--break-lock').exit_code == 0
    assert not (home / 'lock').exists()


def test_changed_manifest_cannot_resume_over_partial_adoption(old_far, monkeypatch):
    dest = old_far
    with monkeypatch.context() as patch:
        patch.setattr(A, '_ingest', lambda *a, **kw: (_ for _ in ()).throw(OSError('interrupted')))
        assert cli(dest, '--yes').exit_code == 1
    tree = A.find_apply_tree(dest)
    from dataclasses import replace
    with pytest.raises(ValueError, match='another manifest'):
        A.adopt_apply_tree(replace(tree, created='20990101T000000Z'))
    assert (dest / '.mmw/manifest.jsonl').exists()


def test_bad_manifest_is_a_readable_cli_error_without_writes(far):  # noqa: F811
    *_rest, dest = far
    record = dest / '.mmw/manifest.jsonl'
    record.write_bytes(b'{"no": "header"}\n')
    before = fingerprint(dest)
    result = cli(dest, '--dry-run')
    assert result.exit_code == 1 and 'cannot read mirror manifest' in result.output
    assert fingerprint(dest) == before


def test_tui_bad_look_cannot_adopt_then_can_repair_and_retry(old_far):
    dest = old_far
    deb = dest / next(p for p in _tree(dest) if p.endswith('.deb'))
    body = deb.read_bytes()
    deb.write_bytes(b'wrong')
    async def run():
        tui = SiteApp(None)
        async with tui.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: tui.screen.query('#start-upgrade') and tui.screen.query_one('#start-upgrade').is_mounted)
            tui.screen.query_one('#start-upgrade').press()
            await until(pilot, lambda: tui.screen.query('#adopt-path'))
            tui.screen.query_one('#adopt-path', Input).value = str(dest)
            tui.screen.query_one('#btn-adopt-look').press()
            await until(pilot, lambda: 'differs' in str(tui.screen.query_one('#adopt-report').render()))
            tui.screen.query_one('#btn-adopt-go').press()
            await pilot.pause()
            assert type(tui.screen).__name__ == 'AdoptPage'
            assert not H.root_home(dest).exists()
            deb.write_bytes(body)
            tui.screen.query_one('#btn-adopt-look').press()
            await until(pilot, lambda: 'Checked ' in str(tui.screen.query_one('#adopt-report').render())
                        and 'differs' not in str(tui.screen.query_one('#adopt-report').render()))
            tui.screen.query_one('#btn-adopt-go').press()
            await until(pilot, lambda: type(tui.screen).__name__ == 'AdoptRunPage' and tui.screen.finished)
            assert tui.screen.succeeded, str(tui.screen.query_one('#adopt-out').render())
            tui.screen.query_one('#btn-adopt-close').press()
            await until(pilot, lambda: type(tui.screen).__name__ == 'SiteHomePage' and tui.screen.query('#site-repos')
                        and tui.screen.query_one('#site-repos').row_count)
    asyncio.run(run())
    assert str(dest) in recents.known()


@pytest.mark.parametrize('row', [
    {'p': '../outside', 'c': 'a' * 64},
    {'p': 'h/file', 'c': 'invalid'},
    {'p': 'h/file', 'c': None},
    [],
])
def test_invalid_manifest_rows_do_not_create_a_site(far, row):  # noqa: F811
    *_rest, dest = far
    record = dest / '.mmw/manifest.jsonl'
    first = record.read_bytes().splitlines()[0]
    record.write_bytes(first + b'\n' + json.dumps(row).encode() + b'\n')
    before = fingerprint(dest)
    result = cli(dest, '--dry-run')
    assert result.exit_code == 1
    assert fingerprint(dest) == before
    assert not H.root_home(dest).exists()


def test_partial_adoption_can_resume_in_a_new_process(old_far, monkeypatch):
    import subprocess
    import sys
    dest = old_far
    from mmw.site import ops
    with monkeypatch.context() as patch:
        real = ops._render
        def stopped(*args, **kwargs):
            real(*args, **kwargs)
            raise OSError('interrupted after importing points')
        patch.setattr(ops, '_render', stopped)
        assert cli(dest, '--yes').exit_code == 1
    result = subprocess.run([str(Path(sys.executable).with_name('mmw')), 'site', 'adopt', str(dest), '--yes'],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not (H.root_home(dest) / 'adoption.json').exists()
    with SiteDB(H.root_home(dest)) as db:
        assert len(db.applied()) == 1 and db.repo_states()[0].serving


def test_tui_can_retry_after_an_io_failure_without_leaving_the_workflow(old_far, monkeypatch):
    from mmw.site import ops
    dest = old_far
    real = ops._render
    failed = False
    def stop_once(*args, **kwargs):
        nonlocal failed
        real(*args, **kwargs)
        if not failed:
            failed = True
            raise OSError('interrupted while finishing adoption')
    monkeypatch.setattr(ops, '_render', stop_once)
    async def run():
        tui = SiteApp(None)
        async with tui.run_test(size=(120, 40)) as pilot:
            for attempt in (1, 2):
                await until(pilot, lambda: type(tui.screen).__name__ == 'StartPage' and tui.screen.query('#start-upgrade'))
                tui.screen.query_one('#start-upgrade').press()
                await until(pilot, lambda: tui.screen.query('#adopt-path'))
                tui.screen.query_one('#adopt-path', Input).value = str(dest)
                tui.screen.query_one('#btn-adopt-look').press()
                await until(pilot, lambda: 'Checked ' in str(tui.screen.query_one('#adopt-report').render()))
                tui.screen.query_one('#btn-adopt-go').press()
                await until(pilot, lambda: type(tui.screen).__name__ == 'AdoptRunPage' and tui.screen.finished)
                assert tui.screen.succeeded == (attempt == 2), str(tui.screen.query_one('#adopt-out').render())
                tui.screen.query_one('#btn-adopt-close').press()
            await until(pilot, lambda: type(tui.screen).__name__ == 'SiteHomePage' and tui.screen.query('#site-history')
                        and tui.screen.query_one('#site-history').row_count)
    asyncio.run(run())
    with SiteDB(H.root_home(dest)) as db:
        assert len(db.applied()) == 1


@pytest.mark.parametrize('changed', [dict(network=None), dict(created='not-a-stamp'), dict(created_label=[])])
def test_invalid_header_values_are_rejected_by_look_without_writes(far, changed):  # noqa: F811
    *_rest, dest = far
    record = dest / '.mmw/manifest.jsonl'
    lines = record.read_bytes().splitlines()
    head = json.loads(lines[0])
    head.update(changed)
    record.write_bytes(json.dumps(head).encode() + b'\n' + b'\n'.join(lines[1:]) + b'\n')
    before = fingerprint(dest)
    result = cli(dest, '--dry-run')
    assert result.exit_code == 1 and 'cannot read mirror manifest' in result.output
    assert fingerprint(dest) == before


def test_old_dot_relative_paths_normalize_before_lookup(far):  # noqa: F811
    *_rest, dest = far
    record = dest / '.mmw/manifest.jsonl'
    lines = record.read_bytes().splitlines()
    rows = []
    for line in lines[1:]:
        row = json.loads(line)
        row['p'] = './' + row['p']
        rows.append(json.dumps(row).encode())
    record.write_bytes(lines[0] + b'\n' + b'\n'.join(rows) + b'\n')
    before = fingerprint(dest)
    look = cli(dest, '--dry-run')
    assert look.exit_code == 0, look.output
    assert fingerprint(dest) == before
    converted = cli(dest, '--yes')
    assert converted.exit_code == 0, converted.output
