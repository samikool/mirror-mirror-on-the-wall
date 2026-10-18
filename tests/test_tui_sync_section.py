"""Sync inside MmwApp: start from Repositories, browse while it runs, quit asks."""
import asyncio

from textual.widgets import ListView

from mmw.core import config
from mmw.core.downloader import MirrorSummary
from mmw.tui import repositories as R
from mmw.tui import sync as sync_mod
from mmw.tui.app import MmwApp
from mmw.tui.sync import SyncView
from mmw.tui.ui.dialog import ConfirmDialog
from waiting import settle, until

SRC = "https://deb.example.com/debian"


def _mirror():
    return config.MirrorConfig(name="debian-bookworm-main-amd64", provider="apt", source=SRC,
                               source_url=SRC, arch="amd64", suite="bookworm", component="main")


async def _fake_sync_mirror(mirror, *, stop_event, events=None, **_):
    if events and events.started:
        events.started(10)
    while not stop_event.is_set():
        await asyncio.sleep(0.02)
    return MirrorSummary(mirror=mirror.name, stopped=True)


async def _settle(pilot, n=6):
    """Until no work is in flight — see tests/waiting.py. The count is ignored."""
    await settle(pilot)


def _run(tmp_path, monkeypatch, check):
    cfg = config.Config(
        settings=config.Settings(mirror_dir=tmp_path / "pool", max_connections=2),
        sources=[config.SourceConfig(url=SRC, suites=["bookworm"], components=["main"],
                                     arches=["amd64"])],
    )
    config.save(cfg, tmp_path)
    monkeypatch.setattr(R, "resolve_selection", lambda c, sel, **kw: ([_mirror()], []))
    monkeypatch.setattr(sync_mod, "sync_mirror", _fake_sync_mirror)

    async def go():
        app = MmwApp(cfg, tmp_path, initial_section="repositories")
        app.notes = []
        app.notify = lambda m, **_: app.notes.append(m)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            await check(app, pilot)
            # leave nothing running
            if app.active_sync is not None:
                app.active_sync.stop()
    asyncio.run(go())


async def _start(app, pilot):
    app.screen.query_one("#repo-list", ListView).focus()
    await pilot.press("s")
    await until(pilot, lambda: app.active_sync is not None and app.active_sync.running)
    return
    raise AssertionError("sync never started")


def test_s_starts_a_sync_in_the_sync_section(tmp_path, monkeypatch):
    async def check(app, pilot):
        await _start(app, pilot)
        await _settle(pilot)
        assert app.current_mode == "sync"
        assert app.screen.query(SyncView)
    _run(tmp_path, monkeypatch, check)


def test_browsing_keeps_it_running_and_the_sidebar_shows_progress(tmp_path, monkeypatch):
    async def check(app, pilot):
        await _start(app, pilot)
        await pilot.press("1")
        await _settle(pilot)
        await pilot.pause(0.7)
        assert app.current_mode == "repositories"
        assert app.active_sync.running
        assert "0/1" in str(app.screen.query_one("#nav-sync").render())
    _run(tmp_path, monkeypatch, check)


def test_a_second_sync_is_refused_while_one_runs(tmp_path, monkeypatch):
    async def check(app, pilot):
        await _start(app, pilot)
        first = app.active_sync
        await pilot.press("1")
        await _settle(pilot)
        app.screen.query_one("#repo-list", ListView).focus()
        await pilot.press("s")
        await _settle(pilot)
        await pilot.pause(0.2)
        assert app.active_sync is first
        assert any("already running" in n for n in app.notes)
    _run(tmp_path, monkeypatch, check)


def test_quit_while_syncing_asks_and_no_keeps_it_running(tmp_path, monkeypatch):
    async def check(app, pilot):
        await _start(app, pilot)
        await pilot.press("q")
        await _settle(pilot)
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("escape")
        await _settle(pilot)
        assert app.is_running and app.active_sync.running
    _run(tmp_path, monkeypatch, check)


def test_quit_while_syncing_yes_stops_it_and_exits(tmp_path, monkeypatch):
    async def check(app, pilot):
        await _start(app, pilot)
        view = app.active_sync
        await pilot.press("q")
        await _settle(pilot)
        app.screen.query_one("#confirm").press()
        await _settle(pilot)
        assert view._stop_event.is_set()
    _run(tmp_path, monkeypatch, check)


def test_mmw_sync_opens_the_sync_section_and_starts_with_the_given_mirrors(tmp_path, monkeypatch):
    """`mmw sync [--dry-run]` → MmwApp(start_sync_with=(mirrors, dry_run))."""
    cfg = config.Config(
        settings=config.Settings(mirror_dir=tmp_path / "pool", max_connections=2),
        sources=[config.SourceConfig(url=SRC, suites=["bookworm"], components=["main"],
                                     arches=["amd64"])],
    )
    config.save(cfg, tmp_path)
    monkeypatch.setattr(sync_mod, "sync_mirror", _fake_sync_mirror)

    async def go():
        app = MmwApp(cfg, tmp_path, initial_section="sync",
                     start_sync_with=([_mirror()], True))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            assert app.current_mode == "sync"
            view = app.screen.query_one(SyncView)
            assert view.dry_run
            assert "Dry run" in str(app.screen.query_one("#page-status").render())
            view.stop()
    asyncio.run(go())


def test_cancelling_resolution_stops_the_resolver(tmp_path, monkeypatch):
    import threading
    captured = {}

    def slow_resolve(c, sel, *, progress=None, stop_event=None):
        captured["stop"] = stop_event
        stop_event.wait(10)
        return [], []

    async def check(app, pilot):
        monkeypatch.setattr(R, "resolve_selection", slow_resolve)
        app.screen.query_one("#repo-list", ListView).focus()
        await pilot.press("s")
        await until(pilot, lambda: "stop" in captured)
        assert isinstance(captured["stop"], threading.Event) and not captured["stop"].is_set()
        app.cancel_resolution()
        assert captured["stop"].is_set()
        await until(pilot, lambda: not app._resolving)

    _run(tmp_path, monkeypatch, check)
