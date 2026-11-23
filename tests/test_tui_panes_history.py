"""History at a glance: the Sources pane's History line, the Networks pane's
Far side has line, and the same summaries in `mmw history`."""
import asyncio
import re

from typer.testing import CliRunner

from mmw.cli import app as cli
from mmw.core.format import fmt_size
from mmw.core.pool import Pool
from test_history_rows import lab  # noqa: F401
from test_snapshot_tui import _run
from test_transfer_repository_selection import synced  # noqa: F401
from waiting import shows, until


def _pane_text(screen):
    return ' '.join(str(w.render()) for w in screen.query('#net-panes Label'))


def test_sources_pane_shows_history(lab):  # noqa: F811
    from mmw.tui.app import MmwApp
    cfg, oak, md, unit, tmp_path = lab
    with Pool(md, read_only=True) as pool:
        size = fmt_size(pool.points_footprint([p.id for p in pool.repository_points(unit.source) if p.held]))

    async def go():
        app = MmwApp(cfg, tmp_path, initial_section='repositories')
        async with app.run_test(size=(140, 40)) as pilot:
            await until(pilot, lambda: app.screen.query('#repo-list'))
            app.screen.query_one('#repo-list').focus()
            await until(pilot, lambda: app.screen.highlighted() is not None)
            text = await shows(pilot, lambda: app.screen.query_one('#repo-detail').render(), '4 kept')
            assert re.search(rf'History\s+stable 04 Nov · 4 kept · {re.escape(size)} · keep all \(default\)',
                             str(text)), text
    asyncio.run(go())


def test_networks_pane_shows_the_far_side(lab):  # noqa: F811
    cfg, oak, md, unit, tmp_path = lab
    with Pool(md, read_only=True) as pool:
        newest = max(p.id for p in pool.repository_points(unit.source))
        size = fmt_size(pool.points_footprint([newest], unsent_to='oak'))

    async def check(app, pilot):
        text = await shows(pilot, lambda: _pane_text(app.screen), 'newer here')
        assert re.search(rf'Far side has\s+stable 03 Nov · newer here: 04 Nov \(1 point, {re.escape(size)}\)',
                         text), text
    _run(cfg, tmp_path, check)


def test_networks_pane_far_side_before_any_bundle(synced, tmp_path):  # noqa: F811
    cfg, net, md, units = synced

    async def check(app, pilot):
        text = await shows(pilot, lambda: _pane_text(app.screen), 'Far side has')
        await until(pilot, lambda: 'nothing yet' in _pane_text(app.screen))
        assert re.search(r'Far side has\s+nothing yet · newer here: 01 Nov \(1 point', _pane_text(app.screen)), text
    _run(cfg, tmp_path, check)


def test_cli_history_says_the_same(lab):  # noqa: F811
    cfg, oak, md, unit, tmp_path = lab
    out = CliRunner().invoke(cli, ['--config', str(tmp_path), 'history', '-n', 'oak']).output
    assert out.splitlines()[0].startswith('oak: far side has stable 03 Nov · newer here: 04 Nov (1 point, ')
    assert 'stable 04 Nov · 4 kept · ' in out and 'keep all (default)' in out
