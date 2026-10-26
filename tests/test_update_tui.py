"""The connected TUI offers a newer mmw on launch and comes back updated."""
import asyncio
from pathlib import Path

import pytest

from mmw.core import config
from mmw.core import update as U
from mmw.tui import update_flow as F
from mmw.tui.app import MmwApp
from waiting import until


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.delenv("MMW_NO_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("MMW_UPDATED_FROM", raising=False)


def _cfg(tmp_path):
    return config.Config(settings=config.Settings(mirror_dir=tmp_path / "pool", max_connections=8))


def _newest(monkeypatch, version, current="1.2.0"):
    def fake_start(self, fetch=None):
        self.current = current
        self.newest = version
        self._done.set()
        return self
    monkeypatch.setattr(U.Checker, "start", fake_start)


def _run(tmp_path, check, cfg=None):
    async def go():
        app = MmwApp(cfg or _cfg(tmp_path), tmp_path, initial_section="repositories")
        async with app.run_test(size=(120, 40)) as pilot:
            await check(app, pilot)
    asyncio.run(go())


def _name(app):
    return type(app.screen).__name__


def test_launch_dialog_when_newer(tmp_path, monkeypatch):
    _newest(monkeypatch, "1.3.0")

    async def check(app, pilot):
        await until(pilot, lambda: _name(app) == "UpdateDialog" and app.focused is not None)
        assert "1.3.0" in str(app.screen.query_one("#confirm-title").render())
        assert app.focused.id == "confirm"                            # a minor update: Yes is the default
    _run(tmp_path, check)


def test_major_dialog_defaults_to_not_now(tmp_path, monkeypatch):
    _newest(monkeypatch, "2.0.0", current="1.9.0")

    async def check(app, pilot):
        await until(pilot, lambda: _name(app) == "UpdateDialog" and app.focused is not None)
        assert "major" in str(app.screen.query_one("#confirm-title").render()).lower()
        assert app.focused.id == "cancel"
    _run(tmp_path, check)


def test_no_carries_on_with_a_marker(tmp_path, monkeypatch):
    _newest(monkeypatch, "1.3.0")

    async def check(app, pilot):
        await until(pilot, lambda: _name(app) == "UpdateDialog" and app.screen.query("#confirm"))
        app.screen.query_one("#cancel").press()
        await until(pilot, lambda: _name(app) != "UpdateDialog")
        assert "update available" in app.sub_title.lower()
    _run(tmp_path, check)


def test_yes_updates_and_relaunches(tmp_path, monkeypatch):
    _newest(monkeypatch, "1.3.0")
    monkeypatch.setattr(U, "detect_install", lambda: U.InstallKind("uv-tool", ["true"], "uv"))
    monkeypatch.setattr(U, "run_update", lambda kind, progress=print: (progress("upgraded"), (True, ""))[1])
    execs = []
    monkeypatch.setattr(F.os, "execve", lambda path, argv, env: execs.append((path, argv, env)))

    holder = {}

    async def check(app, pilot):
        holder["app"] = app
        await until(pilot, lambda: _name(app) == "UpdateDialog" and app.screen.query("#confirm"))
        app.screen.query_one("#confirm").press()
        await until(pilot, lambda: getattr(app, "relaunch_after", None) is not None)
        assert execs == []                         # never while the app still owns the terminal
    _run(tmp_path, check)
    F.relaunch_if_updated(holder["app"])          # what `mmw manage` does once app.run() returns
    (path, argv, env) = execs[0]
    assert env["MMW_UPDATED_FROM"] == "1.2.0"


def test_update_refused_while_another_mmw_works(tmp_path, monkeypatch):
    _newest(monkeypatch, "1.3.0")
    monkeypatch.setattr(U, "busy_elsewhere", lambda mirror_dir: "another mmw is running (it holds the pool)")
    monkeypatch.setattr(U, "run_update", lambda *a, **k: pytest.fail("updated while busy"))

    async def check(app, pilot):
        await until(pilot, lambda: _name(app) == "UpdateDialog" and app.screen.query("#confirm"))
        app.screen.query_one("#confirm").press()
        await until(pilot, lambda: any("another mmw is running" in str(n.message) for n in app._notifications))
    _run(tmp_path, check)


def test_toast_after_relaunch(tmp_path, monkeypatch):
    monkeypatch.setenv("MMW_UPDATED_FROM", "1.1.0")
    monkeypatch.setattr(U.Checker, "start", lambda self, fetch=None: self)

    async def check(app, pilot):
        await until(pilot, lambda: any("Updated to" in str(n.message) and "1.1.0" in str(n.message)
                                       for n in app._notifications))
        import os
        assert "MMW_UPDATED_FROM" not in os.environ
    _run(tmp_path, check)


def test_no_dialog_when_disabled(tmp_path, monkeypatch):
    _newest(monkeypatch, "1.3.0")
    monkeypatch.setenv("MMW_NO_UPDATE_CHECK", "1")

    async def check(app, pilot):
        await pilot.pause(0.3)
        assert _name(app) != "UpdateDialog"
    _run(tmp_path, check)


def test_a_failed_far_update_still_applies(tmp_path):
    """The far side: Close after a failed update carries on with the bundle."""
    from mmw.tui.site.app import SiteApp
    carried = []

    async def go():
        app = SiteApp(None)
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: _name(app) == "StartPage")
            offer = U.Offer("1.2.0", "1.3.0", False)
            app.offer_update(offer, run=lambda log: (False, "disk full"), then=lambda: carried.append(1))
            await until(pilot, lambda: _name(app) == "UpdateDialog" and app.screen.query("#confirm"))
            app.screen.query_one("#confirm").press()
            await until(pilot, lambda: _name(app) == "UpdateRunPage" and
                        not app.screen.query_one("#btn-update-close").disabled)
            app.screen.query_one("#btn-update-close").press()
            await until(pilot, lambda: carried)
    asyncio.run(go())
