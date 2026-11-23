"""`mmw history --package NAME` and `mmw site history --package NAME`: when
each version of a package arrived, read from the kept points' indexes."""
import io
import json

from typer.testing import CliRunner

from mmw.cli import app
from mmw.commands import history
from mmw.core import point_diff as pd
from test_point_diff import versions  # noqa: F401
from test_site_diff import site  # noqa: F401
from test_transfer_repository_selection import synced  # noqa: F401


def test_package_events_name_arrivals_changes_and_departures():
    kept = [{('alpha', 'amd64'): '1.0'}, {('alpha', 'amd64'): '1.0', ('alpha', 'arm64'): '1.1'},
            {('beta', 'amd64'): '2.0'}, {('alpha', 'amd64'): '1.2'}]
    got = pd.package_events('alpha', range(4), lambda i: kept[i])
    assert got == [(0, '1.0'), (1, '1.1'), (2, None), (3, '1.2')]
    assert pd.package_events('zeta', range(4), lambda i: kept[i]) == []


def test_package_history_lists_each_version_and_its_first_point(versions):  # noqa: F811
    cfg, net, md, unit, tmp_path = versions
    events = history.package_plan(cfg, 'alpha')
    assert [(e.suite, e.date[:8], e.version) for e in events] == [
        ('stable', '20261102', '1.0'), ('stable', '20261103', '1.1'), ('stable', '20261104', '1.2')]
    result = CliRunner().invoke(app, ['--config', str(tmp_path), 'history', '--package', 'alpha'])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ['alpha  1.0  stable 2026-11-02', 'alpha  1.1  stable 2026-11-03',
                                          'alpha  1.2  stable 2026-11-04']
    gone = CliRunner().invoke(app, ['--config', str(tmp_path), 'history', '--package', 'delta'])
    assert gone.output.splitlines() == ['delta  0.5      stable 2026-11-02', 'delta  removed  stable 2026-11-03']
    as_json = CliRunner().invoke(app, ['--config', str(tmp_path), 'history', '--package', 'gamma', '--json'])
    [row] = json.loads(as_json.output)
    assert (row['suite'], row['date'][:8], row['version']) == ('stable', '20261103', '1.0')


def test_unknown_package_says_so(versions):  # noqa: F811
    cfg, net, md, unit, tmp_path = versions
    result = CliRunner().invoke(app, ['--config', str(tmp_path), 'history', '--package', 'zeta'])
    assert result.exit_code == 1
    assert "zeta isn't in any kept point" in result.output


def test_site_history_package(site):  # noqa: F811
    root, home = site
    result = CliRunner().invoke(app, ['site', 'history', 'ubuntu', '--package', 'alpha', '--root', str(root)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == ['alpha  1.0  jammy 2026-11-01', 'alpha  1.1  jammy 2026-11-02',
                                          'alpha  1.2  jammy 2026-11-03']
    gone = CliRunner().invoke(app, ['site', 'history', 'ubuntu', '--package', 'delta', '--root', str(root)])
    assert gone.output.splitlines()[-1] == 'delta  removed  jammy 2026-11-02'
    missing = CliRunner().invoke(app, ['site', 'history', 'ubuntu', '--package', 'zeta', '--root', str(root)])
    assert missing.exit_code == 1 and "zeta isn't in any kept point" in missing.output


def test_remote_package_history_verb_round_trips(site):  # noqa: F811
    from mmw.site.remote import serve_root_command
    root, home = site
    out = io.BytesIO()
    assert serve_root_command(['package-history', str(root), 'ubuntu', 'gamma'], io.BytesIO(), out) == 0
    [e] = pd.events_from_json(json.loads(out.getvalue().decode().splitlines()[-1]))
    assert (e.suite, e.date[:8], e.version) == ('jammy', '20261102', '1.0')


# ── the find box on both history tables ──────────────────────────────────────

def test_tui_find_box_narrows_the_history_table(versions):  # noqa: F811
    from test_snapshot_tui import _run
    from test_tui_history_table import _open
    from textual.widgets import Input
    from waiting import shows, until
    cfg, net, md, unit, tmp_path = versions

    async def check(app, pilot):
        t = await _open(app, pilot)
        page = app.screen
        assert 'find' in {b.binding.action for b in page.active_bindings.values() if b.binding.show}
        everything = t.row_count
        await pilot.press('slash')
        assert app.focused is page.query_one('#history-find', Input)
        await pilot.press(*'delta', 'enter')
        await until(pilot, lambda: page._found is not None)
        held = [r for r in page._rows if r is not None]
        assert [r.date[:8] for r in held] == ['20261103', '20261102']
        cells = [str(c) for c in t.get_row_at(1)]
        assert cells[-1].startswith('delta removed')
        assert str(t.get_row_at(2)[-1]).startswith('delta 0.5')
        await pilot.press('escape')                       # clears the find: every point again
        await until(pilot, lambda: t.row_count == everything)
        assert page.query_one('#history-find', Input).value == ''
        await pilot.press('slash', *'zeta', 'enter')
        await shows(pilot, lambda: page.query_one('#page-status').render(), "zeta isn't in any kept point")
        assert t.row_count == everything
    _run(cfg, tmp_path, check)


def test_site_tui_find_box_narrows_the_history(site):  # noqa: F811
    import asyncio
    from mmw.site.mset import open_set
    from mmw.tui.site.app import SiteApp
    from textual.widgets import DataTable, Input
    from waiting import until

    async def go():
        app = SiteApp(open_set())
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: type(app.screen).__name__ == 'SiteHomePage')
            page = app.screen
            t = page.query_one('#site-history', DataTable)
            await until(pilot, lambda: t.row_count == 4)
            await pilot.press('slash')
            assert app.focused is page.query_one('#site-find', Input)
            await pilot.press(*'alpha', 'enter')
            await until(pilot, lambda: page._found is not None)
            assert t.row_count == 4                       # alpha changed in every point
            await pilot.press('escape')
            await pilot.press('slash', *'gamma', 'enter')
            await until(pilot, lambda: page._found is not None and t.row_count == 2)
            assert str(t.get_row_at(1)[1]) == 'gamma 1.0'
            track, point, _mark = page.selected_point()   # the cursor names the found point, not another
            assert point.date[:8] == '20261102'
            await pilot.press('escape')
            await until(pilot, lambda: t.row_count == 4)
    asyncio.run(go())
