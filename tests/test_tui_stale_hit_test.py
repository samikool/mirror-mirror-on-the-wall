"""A click landing on a widget removed since the last frame — a toast that has
just timed out — must not crash the app. Textual's text selection took the
stale widget from the old frame's map and read its missing parent."""
import asyncio

from textual import events
from textual.widgets._toast import Toast

from mmw.tui.app import MmwApp
from test_bundle_verify_gate import _cfg_with_incomplete_mirror
from waiting import settle, until


def test_a_click_on_a_toast_that_just_went_away_is_ignored(tmp_path):
    cfg = _cfg_with_incomplete_mirror(tmp_path)

    async def scenario():
        app = MmwApp(cfg, tmp_path, initial_section="publish")
        async with app.run_test(size=(120, 40), notifications=True) as pilot:
            await settle(pilot)
            app.notify("going away", severity="warning", timeout=60)
            await until(pilot, lambda: app.screen.query(Toast))
            toast = app.screen.query_one(Toast)
            x, y = toast.region.offset
            await toast.remove()                 # gone, but still in the drawn map
            screen = app.screen
            assert screen.get_widget_and_offset_at(x + 1, y + 1)[0] is not toast
            screen._forward_event(events.MouseDown(
                None, x + 1, y + 1, 0, 0, 1, False, False, False, screen_x=x + 1, screen_y=y + 1))
            await settle(pilot)
            assert app.is_running

    asyncio.run(scenario())
