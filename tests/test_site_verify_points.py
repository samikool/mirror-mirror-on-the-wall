"""Far-side verification checks history, including points not being served."""
import io

import pytest
from typer.testing import CliRunner

from mmw.cli import app
from mmw.site import home as H, recents
from mmw.site.db import SiteDB
from mmw.site.roots import Roots
from mmw.site.timeline import import_rows
from site_apt_rows import Repo


@pytest.fixture
def site_point(tmp_path, monkeypatch):
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    root = tmp_path / 'srv'
    home = H.init_root(root, name='main', set_id='s1', network='oak')
    recents.remember(str(root))
    return root, home


def record(site_point, mixed):
    root, home = site_point
    r = Repo()
    r.deb('pool/alpha.deb')
    r.suite('jammy', '21 Apr 2022 17:16:08 UTC', ['pool/alpha.deb'], compress='')
    if mixed:
        r.put('dists/jammy/main/binary-amd64/Packages', b'Package: changed\n\n')
    roots = Roots(H.load_site(home))
    store = roots.store(roots.primary)
    with SiteDB(home) as db:
        for cs, data in r.blobs.items():
            store.add_stream(io.BytesIO(data), expect=cs)
        import_rows(db, store, 'https://ex.org/debian', 'debian', [r.base], r.rows,
                    {c: len(b) for c, b in r.blobs.items()}, '20220421T180000Z', None, set())
        db.set_meta('timeline', '1')


@pytest.mark.parametrize('mixed', [False, True])
def test_site_verify_points(site_point, mixed):
    record(site_point, mixed)
    result = CliRunner().invoke(app, ['site', 'verify', '--root', str(site_point[0]), '--points'])
    assert result.exit_code == int(mixed), result.output
    assert ('jammy 2022-04-21: main/binary-amd64/Packages does not match its Release'
            if mixed else 'every kept point is consistent') in result.output


def test_site_tui_check_points(site_point):
    import asyncio
    from mmw.site.mset import open_set
    from mmw.tui.site.app import SiteApp
    from waiting import until, shows
    record(site_point, True)

    async def go():
        app = SiteApp(open_set())
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: type(app.screen).__name__ == 'SiteHomePage')
            await pilot.press('v')
            await until(pilot, lambda: app.screen.query('#btn-verify-points'))
            await pilot.click('#btn-verify-points')
            await until(pilot, lambda: type(app.screen).__name__ == 'CheckPointsPage')
            await shows(pilot, lambda: app.screen.query_one('#points-result').render(), 'does not match its Release')
    asyncio.run(go())
