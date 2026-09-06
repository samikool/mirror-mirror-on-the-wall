"""Picking a network's publish root instead of typing it.

The publish root is an absolute path to somewhere on a fileserver; typing it
blind is exactly the case `PathPickModal` exists for. The picker is shared with
the key-directory scan and the config loader — one modal, three callers.
"""
import asyncio

from textual.widgets import Button, Input

from mmw.core import config
from mmw.tui.network_screens import AddNetworkScreen
from mmw.tui.path_pick import PathPickModal


def _mirror():
    return config.MirrorConfig(
        name="m", provider="apt", source="https://x/y", source_url="https://x/y",
        arch="arm64", suite="noble", component="main")


async def _settle(pilot, tries: int = 25):
    for _ in range(tries):
        await pilot.pause()


def _run(scenario):
    asyncio.run(scenario())


class _Harness:
    """Mounts AddNetworkScreen on a bare app."""

    def __new__(cls):
        from textual.app import App, ComposeResult

        class _App(App):
            def compose(self) -> ComposeResult:
                yield from ()

            def on_mount(self):
                self.push_screen(AddNetworkScreen([_mirror()]))

        return _App()


def test_browsing_fills_in_the_publish_root(tmp_path):
    (tmp_path / "srv" / "lab").mkdir(parents=True)

    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#btn-browse-root", Button).press()
            await _settle(pilot)

            picker = pilot.app.screen
            assert isinstance(picker, PathPickModal)
            picker.query_one("#path-input", Input).value = str(tmp_path / "srv" / "lab")
            picker.query_one("#btn-use", Button).press()
            await _settle(pilot)

            assert app.screen.query_one("#inp-root", Input).value == str(tmp_path / "srv" / "lab")

    _run(scenario)


def test_cancelling_the_browser_leaves_a_typed_root_alone(tmp_path):
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#inp-root", Input).value = "/already/typed"
            await pilot.pause()
            app.screen.query_one("#btn-browse-root", Button).press()
            await _settle(pilot)
            pilot.app.screen.query_one("#btn-cancel", Button).press()
            await _settle(pilot)

            assert app.screen.query_one("#inp-root", Input).value == "/already/typed"

    _run(scenario)


def test_the_preview_follows_a_browsed_root(tmp_path):
    """`root/name` is what actually gets published — it must update when the
    root arrives from the picker, not only when it is typed."""
    (tmp_path / "srv").mkdir()

    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#inp-name", Input).value = "lab"
            await pilot.pause()
            app.screen.query_one("#btn-browse-root", Button).press()
            await _settle(pilot)
            picker = pilot.app.screen
            picker.query_one("#path-input", Input).value = str(tmp_path / "srv")
            picker.query_one("#btn-use", Button).press()
            await _settle(pilot)

            preview = str(app.screen.query_one("#preview").render())
            assert str(tmp_path / "srv") in preview and "lab" in preview

    _run(scenario)


def test_the_base_path_need_not_contain_the_network_yet(tmp_path):
    """Only the *base* is picked; the network directory below it is created by
    the publish, so the picker must not require it to exist."""
    (tmp_path / "srv").mkdir()

    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#btn-browse-root", Button).press()
            await _settle(pilot)
            picker = pilot.app.screen
            picker.query_one("#path-input", Input).value = str(tmp_path / "srv")
            picker.query_one("#btn-use", Button).press()
            await _settle(pilot)

            step = app.screen.query_one("#inp-root", Input).value
            assert step == str(tmp_path / "srv")
            assert not (tmp_path / "srv" / "lab").exists(), "picker must not create anything"

    _run(scenario)
