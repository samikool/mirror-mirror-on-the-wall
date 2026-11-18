"""The network More action shares the CLI's history preparation/application."""
import asyncio

from textual.widgets import Input, OptionList

from mmw.core import config, points
from mmw.core.pool import Pool
from mmw.tui.app import MmwApp
from mmw.tui.ui.widgets import Button
from test_network_history import history
from waiting import settle, until


def test_include_earlier_points_from_network_more(tmp_path, monkeypatch):
    from mmw.commands import network_history as command
    cfg, old = history(tmp_path)
    config.save(cfg, tmp_path)
    called = []
    real = command.include

    def include(cfg, plan):
        called.append(plan)
        return real(cfg, plan)
    monkeypatch.setattr(command, 'include', include)
    with Pool(cfg.settings.mirror_dir) as pool:
        before = points.current_points(pool, 'lab')

    async def scenario():
        app = MmwApp(cfg, tmp_path, initial_section='networks')
        async with app.run_test(size=(120, 45)) as pilot:
            await settle(pilot)
            app.screen.query_one('#btn-page-more', Button).press()
            await until(pilot, lambda: bool(app.screen.query('#more-list')))
            options = app.screen.query_one('#more-list', OptionList)
            assert not options.get_option('earlier_points').disabled
            options.highlighted = next(i for i in range(options.option_count)
                                       if options.get_option_at_index(i).id == 'earlier_points')
            await pilot.press('enter')
            await until(pilot, lambda: bool(app.screen.query('#history-since')))
            page = app.screen
            page.query_one('#history-since', Input).value = '2026-10-01'
            page.query_one('#btn-history-look', Button).press()
            await until(pilot, lambda: not page.query_one('#btn-history-add', Button).disabled)
            page.query_one('#btn-history-add', Button).press()
            await until(pilot, lambda: 'Included 1' in str(page.query_one('#history-found').render()))
            assert called
    asyncio.run(scenario())
    with Pool(cfg.settings.mirror_dir) as pool:
        assert old in {p.id for p in pool.network_points('lab')}
        assert points.current_points(pool, 'lab') == before
