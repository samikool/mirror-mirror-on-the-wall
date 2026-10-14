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

from textual.widgets import ListView

from mmw.core import config
from mmw.core.publisher.manifest import ledger_dir
from mmw.tui.app import MmwApp
from mmw.tui.ui.dialog import ConfirmDialog
from mmw.tui.ui.widgets import Button
from waiting import settle


def _cfg(tmp_path) -> config.Config:
    return config.Config(
        settings=config.Settings(mirror_dir=tmp_path / "mirror"),
        networks=[config.NetworkConfig(name="lab", bundle_dir=tmp_path / "bundles")],
    )


def _App(cfg: config.Config, cfg_dir):
    """The Networks section of the one app, on a saved copy of `cfg`."""
    config.save(cfg, cfg_dir)
    return MmwApp(cfg, cfg_dir, initial_section="networks")


async def _settle(pilot, tries: int = 40):
    """Until no work is in flight — see tests/waiting.py. The count is ignored."""
    await settle(pilot)


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
            app.screen.query_one("#net-list", ListView).index = 0
            await _settle(pilot)

            app.screen.query_one("#btn-net-remove", Button).press()
            await _settle(pilot)
            app.screen.query_one("#confirm", Button).press()
            await _settle(pilot)

    asyncio.run(scenario())

    assert config.load(tmp_path).networks == []
    assert not ledger.exists()


def test_cancelling_network_removal_leaves_the_ledger_alone(tmp_path):
    ledger = _make_ledger(tmp_path)
    cfg = _cfg(tmp_path)

    async def scenario():
        app = _App(cfg, tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#net-list", ListView).index = 0
            await _settle(pilot)

            app.screen.query_one("#btn-net-remove", Button).press()
            await _settle(pilot)
            assert isinstance(app.screen, ConfirmDialog)
            app.screen.query_one("#cancel", Button).press()
            await _settle(pilot)

    asyncio.run(scenario())

    assert [n.name for n in config.load(tmp_path).networks] == ["lab"]
    assert ledger.exists()
    assert (ledger / "20260901T000000Z.manifest.jsonl.zst").exists()


def test_removing_a_network_with_no_ledger_does_not_crash(tmp_path):
    """The common case for most removals: no bundle was ever made."""
    cfg = _cfg(tmp_path)

    async def scenario():
        app = _App(cfg, tmp_path)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#net-list", ListView).index = 0
            await _settle(pilot)

            app.screen.query_one("#btn-net-remove", Button).press()
            await _settle(pilot)
            app.screen.query_one("#confirm", Button).press()
            await _settle(pilot)

    asyncio.run(scenario())   # must not raise

    assert config.load(tmp_path).networks == []
