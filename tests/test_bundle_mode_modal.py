"""`do_bundle` must ask delta-or-full every time, and the choice must reach
`bundle_network`'s `full` argument — this closes a known
gap: `_execute_bundle` used to hardcode `full=False`, so no user-facing
surface could ever ask for a full re-bundle.

Cancelling the modal is a third, distinct outcome from choosing "Delta" — it
must abort the bundle entirely (bundle_network never called), not silently
behave like declining down to the delta default.
"""
import asyncio
from pathlib import Path

from textual.app import App, ComposeResult

from mmw.core import config
from mmw.core.publisher.results import BundleResult
from mmw.tui.publish_modals import BundleModeModal, BundleProgressModal
from mmw.tui.publish_panel import NetworkPanel
from mmw.tui.widgets import Button


def _cfg(tmp_path: Path) -> config.Config:
    return config.Config(
        settings=config.Settings(mirror_dir=tmp_path / "mirror"),
        networks=[config.NetworkConfig(name="lab", bundle_dir=tmp_path / "bundles")],
    )


class _App(App):
    def __init__(self, cfg: config.Config, cfg_dir: Path):
        super().__init__()
        self._cfg = cfg
        self._cfg_dir = cfg_dir

    def compose(self) -> ComposeResult:
        yield NetworkPanel(self._cfg, self._cfg_dir)


async def _settle(pilot, tries: int = 40):
    for _ in range(tries):
        await pilot.pause()


def test_bundle_mode_modal_buttons_dismiss_with_the_right_value():
    async def scenario():
        result = {}

        class _Host(App):
            def compose(self):
                yield from ()

            def on_mount(self):
                self.run_worker(self._go())

            async def _go(self):
                result["full"] = await self.push_screen_wait(BundleModeModal("lab"))

        host = _Host()
        async with host.run_test() as pilot:
            await _settle(pilot)
            host.screen.query_one("#full", Button).press()
            await _settle(pilot)
        return result["full"]

    assert asyncio.run(scenario()) is True


def test_bundle_mode_modal_delta_button_dismisses_false():
    async def scenario():
        result = {}

        class _Host(App):
            def compose(self):
                yield from ()

            def on_mount(self):
                self.run_worker(self._go())

            async def _go(self):
                result["full"] = await self.push_screen_wait(BundleModeModal("lab"))

        host = _Host()
        async with host.run_test() as pilot:
            await _settle(pilot)
            host.screen.query_one("#delta", Button).press()
            await _settle(pilot)
        return result["full"]

    assert asyncio.run(scenario()) is False


def test_bundle_mode_modal_cancel_dismisses_none():
    async def scenario():
        result = {}

        class _Host(App):
            def compose(self):
                yield from ()

            def on_mount(self):
                self.run_worker(self._go())

            async def _go(self):
                result["full"] = await self.push_screen_wait(BundleModeModal("lab"))

        host = _Host()
        async with host.run_test() as pilot:
            await _settle(pilot)
            await pilot.press("escape")
            await _settle(pilot)
        return result["full"]

    assert asyncio.run(scenario()) is None


def _run_do_bundle(tmp_path, monkeypatch, press_id):
    """Drive the real `do_bundle` (button press, not a direct `_execute_bundle`
    call) through the mode modal, and report what `bundle_network` was
    actually called with."""
    calls = []

    def fake(network, cfg, mirror_dir, out_path, full, level, progress, stop_event,
            mirror_start_callback, compress=True):
        calls.append(full)
        return BundleResult(network_name=network.name, out_path=tmp_path / "x.tar", sent=0)

    monkeypatch.setattr(BundleProgressModal, "set_timer",
                        lambda self, delay, callback: callback())
    monkeypatch.setattr("mmw.core.publisher.bundle.bundle_network", fake)

    cfg = _cfg(tmp_path)

    async def scenario():
        app = _App(cfg, tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            panel = app.query_one(NetworkPanel)
            from textual.widgets import ListView
            panel.query_one("#pub-list", ListView).index = 0
            await _settle(pilot)

            panel.query_one("#btn-bundle", Button).press()
            await _settle(pilot)
            if press_id is not None:
                panel.app.screen.query_one(f"#{press_id}", Button).press()
            else:
                await pilot.press("escape")
            await _settle(pilot, 80)

    asyncio.run(scenario())
    return calls


def test_choosing_full_passes_full_true_to_bundle_network(tmp_path, monkeypatch):
    calls = _run_do_bundle(tmp_path, monkeypatch, "full")
    assert calls == [True]


def test_choosing_delta_passes_full_false_to_bundle_network(tmp_path, monkeypatch):
    calls = _run_do_bundle(tmp_path, monkeypatch, "delta")
    assert calls == [False]


def test_cancelling_the_mode_modal_never_calls_bundle_network(tmp_path, monkeypatch):
    calls = _run_do_bundle(tmp_path, monkeypatch, None)
    assert calls == []
