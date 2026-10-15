"""Plan 4 review fixes: Esc never silently kills a running sync; a stopped sync
says so; a new sync waits for a stopped one to drain."""
import asyncio
import threading

from mmw.core import config
from mmw.core.downloader import MirrorSummary
from mmw.tui import sync as sync_mod
from mmw.tui.app import MmwApp
from mmw.tui.sync import SyncView
from waiting import until

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


def _cfg(tmp_path):
    return config.Config(settings=config.Settings(mirror_dir=tmp_path / "pool", max_connections=2))


def _mmw_sync(tmp_path):
    """What `mmw sync` opens: the Sync section, starting the given mirrors."""
    return MmwApp(_cfg(tmp_path), tmp_path, initial_section="sync",
                  start_sync_with=([_mirror()], False))


async def _until(pilot, cond, tries=None):
    """`tries` is ignored: see tests/waiting.py — wait for the thing, against a clock."""
    await until(pilot, cond)

def test_esc_in_mmw_sync_does_not_stop_it(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_mod, "sync_mirror", _fake_sync_mirror)

    async def go():
        app = _mmw_sync(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await _until(pilot, lambda: app.active_sync is not None and app.active_sync.running)
            page = app.screen
            await pilot.press("escape")
            await pilot.press("escape")
            for _ in range(5):
                await pilot.pause()
            assert app.screen is page and app.is_running
            assert app.active_sync.running
            app.active_sync.stop()
    asyncio.run(go())


def test_a_stopped_sync_says_stopped_not_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_mod, "sync_mirror", _fake_sync_mirror)

    async def go():
        app = _mmw_sync(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await _until(pilot, lambda: app.active_sync is not None and app.active_sync.running)
            page = app.screen
            app.active_sync.stop()

            def status() -> str:
                return str(page.query_one("#page-status").render()).lower()
            # The view sets `_complete_shown` and then *posts* Completed; the
            # status changes when the section handles it — wait for that.
            await _until(pilot, lambda: page.view._complete_shown and "syncing" not in status())
            assert "stopped" in status() and "complete" not in status()
    asyncio.run(go())


def test_draining_blocks_a_new_start_until_cleanup_finishes(tmp_path, monkeypatch):
    release = threading.Event()

    class _SlowExecutor:
        def shutdown(self, wait=True, cancel_futures=False):
            release.wait(5)

    view = SyncView(_cfg(tmp_path), mirrors=[])
    view._executor = _SlowExecutor()
    assert not view.draining
    view.stop()
    assert view.draining
    release.set()
    view._drained.wait(5)
    assert not view.draining


def test_the_sidebar_progress_tick_survives_a_mode_switch_in_progress(tmp_path, monkeypatch):
    """The 2 Hz label update ran mid switch_mode, when the new mode's stack
    was momentarily empty, and crashed the app with ScreenStackError."""
    from textual.app import ScreenStackError

    from mmw.tui.app import MmwApp

    def empty_stack(self):
        raise ScreenStackError("No screens on stack")

    app = MmwApp(_cfg(tmp_path), tmp_path)
    monkeypatch.setattr(MmwApp, "screen", property(empty_stack))
    app._sync_label()                       # must not raise
