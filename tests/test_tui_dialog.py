"""ConfirmDialog — the one modal left in the TUI."""
import asyncio

import pytest

from mmw.tui.ui.base import ThemedApp
from mmw.tui.ui.dialog import ConfirmDialog


@pytest.mark.parametrize("action,expected", [("#confirm", True), ("#cancel", False), ("escape", False)])
def test_confirm_dialog_answers(action, expected):
    async def go():
        app = ThemedApp()
        got = []
        async with app.run_test(size=(80, 24)) as pilot:
            async def ask():
                got.append(await app.push_screen_wait(ConfirmDialog("Remove", "Remove [b]x[/b]?")))
            app.run_worker(ask())
            for _ in range(3):
                await pilot.pause()
            assert all(b.region.height == 1 for b in app.screen.query("Button"))
            if action == "escape":
                await pilot.press("escape")
            else:
                app.screen.query_one(action).press()
            for _ in range(3):
                await pilot.pause()
        return got
    assert asyncio.run(go()) == [expected]
