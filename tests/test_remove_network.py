"""Removing a network must also clear its bundle ledger — the per-network
delta bookkeeping under `{mirror_dir}/bundles/{name}/` — so a network added
later under the same name starts fresh instead of silently inheriting stale
delta history from a network that no longer exists.

The ledger is cleared unconditionally (no extra prompt): unlike root/
bundle_dir, it's a handful of small manifest files, this network's own
metadata, never served content and never shared with anything else.
"""
import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import ListView

from mmw.core import config
from mmw.core.publisher.manifest import ledger_dir
from mmw.tui.modals import ConfirmModal
from mmw.tui.publish_panel import NetworkPanel
from mmw.tui.widgets import Button


def _cfg(tmp_path) -> config.Config:
    return config.Config(
        settings=config.Settings(mirror_dir=tmp_path / "mirror"),
        networks=[config.NetworkConfig(name="lab", bundle_dir=tmp_path / "bundles")],
    )


class _App(App):
    def __init__(self, cfg: config.Config, cfg_dir):
        super().__init__()
        self._cfg = cfg
        self._cfg_dir = cfg_dir

    def compose(self) -> ComposeResult:
        yield NetworkPanel(self._cfg, self._cfg_dir)


async def _settle(pilot, tries: int = 40):
    for _ in range(tries):
        await pilot.pause()


def _make_ledger(tmp_path, name="lab") -> Path:
    mirror_dir = tmp_path / "mirror"
    ledger = ledger_dir(mirror_dir, name)
    ledger.mkdir(parents=True)
    (ledger / "20260901T000000Z.manifest.jsonl.zst").write_bytes(b"fake-manifest")
    return ledger


def test_removing_a_network_clears_its_bundle_ledger(tmp_path):
    ledger = _make_ledger(tmp_path)
    cfg = _cfg(tmp_path)

    async def scenario():
        app = _App(cfg, tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            panel = app.query_one(NetworkPanel)
            panel.query_one("#pub-list", ListView).index = 0
            await _settle(pilot)

            panel.query_one("#btn-remove-network", Button).press()
            await _settle(pilot)
            panel.app.screen.query_one("#confirm", Button).press()
            await _settle(pilot)

    asyncio.run(scenario())

    assert cfg.networks == []
    assert not ledger.exists()


def test_cancelling_network_removal_leaves_the_ledger_alone(tmp_path):
    ledger = _make_ledger(tmp_path)
    cfg = _cfg(tmp_path)

    async def scenario():
        app = _App(cfg, tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            panel = app.query_one(NetworkPanel)
            panel.query_one("#pub-list", ListView).index = 0
            await _settle(pilot)

            panel.query_one("#btn-remove-network", Button).press()
            await _settle(pilot)
            assert isinstance(panel.app.screen, ConfirmModal)
            panel.app.screen.query_one("#cancel", Button).press()
            await _settle(pilot)

    asyncio.run(scenario())

    assert [n.name for n in cfg.networks] == ["lab"]
    assert ledger.exists()
    assert (ledger / "20260901T000000Z.manifest.jsonl.zst").exists()


def test_removing_a_network_with_no_ledger_does_not_crash(tmp_path):
    """The common case for most removals: no bundle was ever made."""
    cfg = _cfg(tmp_path)

    async def scenario():
        app = _App(cfg, tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            panel = app.query_one(NetworkPanel)
            panel.query_one("#pub-list", ListView).index = 0
            await _settle(pilot)

            panel.query_one("#btn-remove-network", Button).press()
            await _settle(pilot)
            panel.app.screen.query_one("#confirm", Button).press()
            await _settle(pilot)

    asyncio.run(scenario())   # must not raise

    assert cfg.networks == []
