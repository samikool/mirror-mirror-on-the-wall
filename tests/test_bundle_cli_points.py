"""Choosing what a bundle carries: --history / --newest and --full on the
command line, the network's `history`, and the TUI's transfer page."""
import asyncio

import pytest
from typer.testing import CliRunner

from mmw import cli
from mmw.core import config

from test_bundle_mmw import _mmw
from test_dry_run_totals import two_mirrors  # noqa: F401


@pytest.fixture
def cfg_dir(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    d = tmp_path / "cfg"
    d.mkdir()
    config.save(cfg, d)
    return d, net


def _captured(monkeypatch):
    from mmw.commands import bundle as bundle_mod                   # the command's own name for it
    seen = []
    real = bundle_mod.bundle_network

    def spy(*a, **k):
        seen.append(k)
        return real(*a, **k)
    monkeypatch.setattr(bundle_mod, "bundle_network", spy)
    return seen


@pytest.mark.parametrize("flags,history", [([], None), (["--history"], True), (["--newest"], False)])
def test_history_flags_reach_the_bundle(cfg_dir, tmp_path, monkeypatch, flags, history):
    d, net = cfg_dir
    seen = _captured(monkeypatch)
    r = CliRunner().invoke(cli.app, ["--config", str(d), "bundle", "-n", net.name, "--out", str(tmp_path / "o"),
                                     "--allow-incomplete", *flags])
    assert r.exit_code == 0, r.output
    assert seen[-1].get("history") == history


def test_full_flag(cfg_dir, tmp_path, monkeypatch):
    d, net = cfg_dir
    seen = _captured(monkeypatch)
    r = CliRunner().invoke(cli.app, ["--config", str(d), "bundle", "-n", net.name, "--out", str(tmp_path / "o"),
                                     "--allow-incomplete", "--full"])
    assert r.exit_code == 0, r.output
    assert seen[-1]["from_ref"] is None


def test_history_round_trips_in_config(tmp_path):
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "pool"),
                        networks=[config.NetworkConfig(name="oak", far_side="mmw", history=True)])
    config.save(cfg, tmp_path)
    assert config.load(tmp_path).networks[0].history is True


def test_the_transfer_page_offers_history(two_mirrors, tmp_path):  # noqa: F811
    from textual.widgets import Select

    from test_snapshot_tui import _mmw_net, _run
    from waiting import until
    cfg, net, mirror_dir = _mmw_net(two_mirrors, tmp_path)
    picked = []

    async def check(app, pilot):
        await pilot.press("t")
        await until(pilot, lambda: type(app.screen).__name__ == "BundlePickPage"
                    and app.screen.query("#pick-points"))
        pts = app.screen.query_one("#pick-points", Select)
        assert pts.value == "newest"                                      # the network's own default
        pts.value = "history"
        picked.append(app.screen.choice().history)
    _run(cfg, mirror_dir.parent, check)
    assert picked == [True]
    asyncio  # noqa: B018
