"""Skip intermediate releases: open original site schemas directly in the TUI."""
import asyncio
import json
import os
import sqlite3
from pathlib import Path

import pytest
from textual.widgets import DataTable
from typer.testing import CliRunner

from mmw.cli import app
from mmw.site import home as H, recents
from mmw.site.db import SiteDB
from mmw.site.mset import open_set
from mmw.site.store import Store
from mmw.tui.site.app import SiteApp

from test_site_timeline import REPO, _snap, _put
from waiting import until


@pytest.fixture(params=['v1.3.0', 'v1.4.0', 'v1.5.0'])
def released_site(request, tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    root = tmp_path / 'srv'
    home = H.init_root(root, name='main', set_id='old', network='lab')
    if request.param != 'v1.5.0':
        settings = home / 'site.toml'
        settings.write_text('keep_versions = 2\n' + settings.read_text())
    schema = Path(__file__).with_name('fixtures') / f'site-schema-{request.param}.sql'
    with sqlite3.connect(home / 'site.db') as db:
        db.executescript(schema.read_text())
        for n in (1, 2):
            repo = _snap(n)
            _put(Store(root), repo)
            db.execute('INSERT INTO versions(id,repo,number,label,digest,stamp,snap_label) VALUES (?,?,?,?,?,?,?)',
                       (n, REPO, n, 'debian', f'digest{n}', f'2026110{n}T120000Z', f'label{n}'))
            for path, checksum in repo.rows.items():
                db.execute('INSERT OR IGNORE INTO paths(path) VALUES (?)', (path,))
                db.execute('INSERT OR IGNORE INTO blobs(checksum,size) VALUES (?,?)', (checksum, len(repo.blobs[checksum])))
                db.execute('INSERT INTO version_files SELECT ?, p.id, b.id FROM paths p, blobs b '
                           'WHERE p.path=? AND b.checksum=?', (n, path, checksum))
            if n == 2:
                for path, checksum in repo.rows.items():
                    target = root / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.link(Store(root).path(checksum), target)
        db.execute('INSERT INTO repos(repo,label,bases,serving,held) VALUES (?,?,?,?,1)',
                   (REPO, 'debian', json.dumps([repo.base]), 2))
        if request.param == 'v1.5.0':
            db.execute('INSERT INTO tracks(id,repo,name,suite,pinned) VALUES (1,?,?,?,2)',
                       (REPO, repo.base + '/dists/bookworm', 'bookworm'))
            for n in (1, 2):
                old = _snap(n)
                checksum = old.rows[old.base + '/dists/bookworm/Release']
                db.execute('INSERT INTO points(id,track,ident,date,stamp,snap_label) VALUES (?,1,?,?,?,?)',
                           (n, 'release:' + checksum, f'2026110{n}T000000Z', f'2026110{n}T120000Z', f'label{n}'))
                db.execute('INSERT INTO point_files SELECT ?,path,blob FROM version_files WHERE version=?', (n, n))
                db.execute('UPDATE versions SET points=? WHERE id=?', (json.dumps([n]), n))
            db.execute("INSERT INTO meta VALUES ('timeline','1')")
    recents.remember(str(root))
    before = {p.relative_to(root): (p.stat().st_ino, p.read_bytes()) for p in root.rglob('*')
              if p.is_file() and '.mmw' not in p.relative_to(root).parts}
    return request.param, root, home, before


def _preserved(site):
    version, root, home, before = site
    assert {p: ((root / p).stat().st_ino, (root / p).read_bytes()) for p in before} == before
    with SiteDB(home) as db:
        assert db.get_meta('timeline') == '1'
        state = db.repo_state(REPO)
        assert state.held and state.serving.number == 2
        track, = [t for t in db.tracks(REPO) if t.name]
        assert [p.date[:8] for p in db.points(track.id)] == ['20261102', '20261101']
        assert db.point(track.pinned).date.startswith('20261102')
        assert {p.snap_label for p in db.points(track.id)} == {'label1', 'label2'}
    if version != 'v1.5.0':
        assert H.load_site(home).keep == 2 and H.load_site(home).auto_clean
        assert (home.with_name('site.before-timelines') / 'site.db').is_file()
    else:
        assert not home.with_name('site.before-timelines').exists()


def test_cli_history_upgrades_original_release_schema_without_an_apply(released_site):
    _, root, _, _ = released_site
    result = CliRunner().invoke(app, ['site', 'history', 'debian', '--root', str(root)])
    assert result.exit_code == 0, result.output
    assert '2026-11-01' in result.output and '2026-11-02' in result.output
    _preserved(released_site)


def test_tui_open_upgrades_original_release_schema_before_an_apply(released_site):
    async def run():
        tui = SiteApp(open_set())
        async with tui.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: type(tui.screen).__name__ in ('ConfirmDialog', 'SiteHomePage'))
            # Old schemas convert on the first look, and their notice arrives
            # after the cards; wait for the actual conversion before clicking.
            if released_site[0] != 'v1.5.0':
                await until(pilot, lambda: type(tui.screen).__name__ == 'ConfirmDialog'
                            and tui.screen.query('#confirm') and tui.screen.query_one('#confirm').is_mounted)
                tui.screen.query_one('#confirm').press()
            await until(pilot, lambda: type(tui.screen).__name__ == 'SiteHomePage'
                        and tui.screen.query('#site-history')
                        and tui.screen.query_one('#site-history', DataTable).row_count == 3)
    asyncio.run(run())
    _preserved(released_site)
