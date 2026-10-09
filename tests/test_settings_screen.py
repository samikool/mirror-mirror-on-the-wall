"""`[settings]` is editable from the TUI — before, only by deleting mmw.toml."""
import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Button, Input

from mmw.core import config
from mmw.tui.settings_screen import SettingsScreen


def _run(settings, actions):
    async def scenario():
        class _App(App):
            def compose(self) -> ComposeResult:
                yield from ()

            def on_mount(self):
                self.push_screen(SettingsScreen(settings), self._capture)

            def _capture(self, value):
                self.result = value

        app = _App()
        app.result = "untouched"
        async with app.run_test(size=(100, 30)) as pilot:
            for _ in range(10):
                await pilot.pause()
            actions(app.screen)
            await pilot.pause()
            app.screen.query_one("#btn-settings-save", Button).press()
            for _ in range(10):
                await pilot.pause()
        return app.result
    return asyncio.run(scenario())


BASE = config.Settings(mirror_dir=Path("/srv/pool"), max_connections=8)


def test_both_settings_are_saved():
    def act(screen):
        screen.query_one("#settings-mirror-dir", Input).value = "/data/pool"
        screen.query_one("#settings-max-connections", Input).value = "32"
    result = _run(BASE, act)
    assert result == config.Settings(mirror_dir=Path("/data/pool"), max_connections=32)


def test_unchanged_save_is_equal():
    assert _run(BASE, lambda s: None) == BASE


def test_zero_connections_is_refused():
    def act(screen):
        screen.query_one("#settings-max-connections", Input).value = "0"
    assert _run(BASE, act) == "untouched"


def test_empty_pool_directory_is_refused():
    def act(screen):
        screen.query_one("#settings-mirror-dir", Input).value = "  "
    assert _run(BASE, act) == "untouched"
