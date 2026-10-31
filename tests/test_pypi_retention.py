"""Network retention: how long releases that left a PyPI unit stay published."""
from datetime import date

import pytest
from pydantic import ValidationError
from textual.widgets import Button, Input, ListView

from mmw.core import config
from mmw.core.config import retention_days
from mmw.providers.pypi.history import in_window
from mmw.tui.network_screens import EditNetworkPage
from test_tui_networks_page import _cfg, _names, _run, _settle
from waiting import until


def test_retention_values():
    assert retention_days("current") == 0 and retention_days("30d") == 30 and retention_days("forever") is None
    for bad in ("0d", "30", "weekly", "-1d", ""):
        with pytest.raises(ValueError):
            retention_days(bad)
    assert config.NetworkConfig(name="n").retention == "90d"
    with pytest.raises(ValidationError, match="retention"):
        config.NetworkConfig(name="n", retention="soon")


def test_retention_window_edges():
    today = date(2026, 10, 18)
    assert in_window("2026-10-09", 10, today)
    assert not in_window("2026-10-08", 10, today)
    assert in_window(None, 0, today) and in_window("2020-01-01", None, today)
    assert not in_window("2026-10-17", 0, today)


def test_network_retention_round_trips(tmp_path):
    cfg = _cfg(tmp_path, "lab")
    cfg.networks[0].retention = "30d"
    config.save(cfg, tmp_path)
    assert config.load(tmp_path).networks[0].retention == "30d"
    cfg.networks[0].retention = "90d"
    config.save(cfg, tmp_path)
    assert "retention =" not in (tmp_path / "mmw.toml").read_text()


async def _edit(app, pilot, value):
    await until(pilot, lambda: app.screen.query("#net-list") and "lab" in _names(app))
    lst = app.screen.query_one("#net-list", ListView)
    lst.index = _names(app).index("lab")
    await _settle(pilot)
    await pilot.press("e")
    await until(pilot, lambda: isinstance(app.screen, EditNetworkPage) and app.screen.query("#btn-save"))
    page = app.screen
    page.query_one("#inp-retention", Input).value = value
    page.query_one("#btn-save", Button).press()
    return page


def test_tui_edits_retention(tmp_path, monkeypatch):
    async def check(app, pilot):
        await _edit(app, pilot, "7d")
        await until(pilot, lambda: not isinstance(app.screen, EditNetworkPage))
        await until(pilot, lambda: config.load(tmp_path).networks[0].retention == "7d")
        await _edit(app, pilot, "soon")
        await until(pilot, lambda: any("retention" in str(n) for n in app.notes))
        assert isinstance(app.screen, EditNetworkPage)
        assert config.load(tmp_path).networks[0].retention == "7d"
    _run(tmp_path, check, cfg=_cfg(tmp_path, "lab"), monkeypatch=monkeypatch)


def test_widening_retention_warns_not_retroactive(tmp_path, monkeypatch):
    async def check(app, pilot):
        await _edit(app, pilot, "forever")
        await until(pilot, lambda: any("not retroactive" in str(n) for n in app.notes))
        await until(pilot, lambda: not isinstance(app.screen, EditNetworkPage))
        app.notes.clear()
        await _edit(app, pilot, "current")
        await until(pilot, lambda: config.load(tmp_path).networks[0].retention == "current")
        assert not any("not retroactive" in str(n) for n in app.notes)
    _run(tmp_path, check, cfg=_cfg(tmp_path, "lab"), monkeypatch=monkeypatch)
