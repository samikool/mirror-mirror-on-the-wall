"""The follow-ups deferred from the Publish-section review, each pinned."""
import asyncio
import threading
from types import SimpleNamespace

from textual.widgets import Button

from mmw.core import config
from mmw.tui import sync as sync_mod
from mmw.tui.app import MmwApp
from mmw.tui.sync import SyncView
from mmw.tui.ui.dialog import ConfirmDialog

from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture
from pickers import press_publish
from test_tui_publish_section import _finished, _run, _settle, _until
from waiting import until

SRC = "https://deb.example.com/debian"


# ── 1. a finished sync stops ticking, after one last pool scan ───────────────

def test_a_finished_sync_stops_ticking_after_a_final_pool_scan(tmp_path, monkeypatch):
    from mmw.core.pool import Pool
    mirror = config.MirrorConfig(name="debian-bookworm-main-amd64", provider="apt", source=SRC,
                                 source_url=SRC, arch="amd64", suite="bookworm", component="main")

    async def quick(mirror, *, stop_event, events=None, **_):
        from mmw.core.downloader import MirrorSummary
        return MirrorSummary(mirror=mirror.name)
    monkeypatch.setattr(sync_mod, "sync_mirror", quick)
    scans = []
    real_stats = Pool.stats
    monkeypatch.setattr(Pool, "stats", lambda self: (scans.append(1), real_stats(self))[1])
    ticks = []
    real_tick = SyncView._tick
    monkeypatch.setattr(SyncView, "_tick", lambda self: (ticks.append(1), real_tick(self))[1])
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "pool", max_connections=2))

    async def go():
        app = MmwApp(cfg, tmp_path, initial_section="sync", start_sync_with=([mirror], False))
        async with app.run_test(size=(100, 30)) as pilot:
            view = None
            await until(pilot, lambda: app.active_sync is not None
                        and app.active_sync._complete_shown and app.active_sync._final_scan_done)
            view = app.active_sync
            assert view._complete_shown and view._final_scan_done, "no final pool scan"
            assert scans, "the footer's totals were never re-read at the end"
            before = len(ticks)
            await pilot.pause(0.3)
            assert len(ticks) == before, "still ticking after the sync finished"
    asyncio.run(go())


# ── 2. Stop pressed during certs does not unmake a finished publish ─────────

def test_stop_during_certs_keeps_a_finished_publish_finished(two_mirrors, monkeypatch):  # noqa: F811
    import mmw.core.certs as certs
    cfg, net, mirror_dir = two_mirrors
    gate, entered = threading.Event(), threading.Event()
    generate = certs.generate_certs
    calls = 0

    def slow_certs(net, cfg):
        nonlocal calls
        calls += 1
        if calls == 1:
            # TLS is now part of the portable publish itself. Only block the
            # optional UI follow-up, after the deployment finished.
            return generate(net, cfg)
        entered.set()
        gate.wait(5)
        return ["example.com"], "k", "c", "r", "m"
    monkeypatch.setattr(certs, "generate_certs", slow_certs)

    async def check(app, pilot):
        await press_publish(app, pilot)
        await _until(pilot, lambda: app.active_transfer is not None)
        view = app.active_transfer
        await _until(pilot, entered.is_set)
        view.stop()
        gate.set()
        await _finished(pilot, view)
        assert view.outcome == "ok", view.summary()
        await view.remove()                       # unmounting it later changes nothing
        assert view.outcome == "ok"
    _run(cfg, mirror_dir.parent, check, certs=True)


# ── 3. b is refused before asking from/to when a transfer runs ────────

def test_b_is_refused_before_the_mode_question_while_busy(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors

    async def check(app, pilot):
        app.active_transfer = SimpleNamespace(running=True, network_name="other",
                                              awaiting_decision=False, progress=lambda: None)
        await pilot.press("t")
        await _settle(pilot)
        assert not app.screen.query("#btn-pick-go"), "asked from/to only to refuse after"
        assert any("Already working on other" in n for n in app.notes)
        app.active_transfer = None
    _run(cfg, mirror_dir.parent, check, bundle_dir=mirror_dir.parent / "usb")


# ── 4. Networks re-reads its detail when a transfer ends while it is shown ──

def test_networks_detail_refreshes_when_a_transfer_ends_while_shown(two_mirrors, monkeypatch):  # noqa: F811
    import mmw.core.publisher as publisher
    from mmw.tui.sections.network_detail import NetworkDetail
    cfg, net, mirror_dir = two_mirrors
    release = threading.Event()

    def held(net, cfg, md, record, stop_event, cb, **_kw):
        release.wait(60)          # released by the test; the cap only stops a hang
        return publisher.NetworkPublishResult(network_name=net.name)
    monkeypatch.setattr(publisher, "publish_network", held)
    shows = []
    real_show = NetworkDetail.show
    monkeypatch.setattr(NetworkDetail, "show",
                        lambda self, n, c, **k: (shows.append(n and n.name), real_show(self, n, c, **k))[1])

    async def check(app, pilot):
        await press_publish(app, pilot)
        await _until(pilot, lambda: app.active_transfer is not None
                     and app.active_transfer._transferring)
        await pilot.press("2")                    # back to Networks, as a user would
        await _until(pilot, lambda: app.current_mode == "networks")
        assert app.current_mode == "networks" and app.active_transfer.running, (
            app.active_transfer.outcome, app.active_transfer.summary())
        before = len(shows)
        release.set()
        await _until(pilot, lambda: not app.active_transfer.running)
        await _until(pilot, lambda: len(shows) > before)
        assert len(shows) > before, "detail not refreshed after the publish finished"
    _run(cfg, mirror_dir.parent, check)


# ── 5 + 6. Stop: disabled once confirmed, and an x key in both sections ─────

def test_x_asks_to_stop_and_the_button_is_disabled_once_confirmed(two_mirrors, monkeypatch):  # noqa: F811
    import mmw.core.publisher as publisher
    cfg, net, mirror_dir = two_mirrors
    release = threading.Event()

    def held(net, cfg, md, record, stop_event, cb, **_kw):
        while not (release.is_set() or stop_event.is_set()):
            stop_event.wait(0.02)
        return publisher.NetworkPublishResult(network_name=net.name)
    monkeypatch.setattr(publisher, "publish_network", held)

    async def check(app, pilot):
        await press_publish(app, pilot)
        await _until(pilot, lambda: app.active_transfer is not None
                     and app.active_transfer._transferring)
        section = app.screen
        await pilot.press("x")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmDialog))
        app.screen.query_one("#confirm").press()
        await _until(pilot, lambda: section.query_one("#btn-transfer-stop", Button).disabled)
        await _until(pilot, lambda: not app.active_transfer.running)
    _run(cfg, mirror_dir.parent, check)


def test_x_asks_to_stop_a_sync_too(tmp_path, monkeypatch):
    from test_tui_sync_section import _fake_sync_mirror, _mirror
    monkeypatch.setattr(sync_mod, "sync_mirror", _fake_sync_mirror)
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "pool", max_connections=2))

    async def go():
        app = MmwApp(cfg, tmp_path, initial_section="sync", start_sync_with=([_mirror()], False))
        async with app.run_test(size=(100, 30)) as pilot:
            await until(pilot, lambda: app.active_sync is not None and app.active_sync.running)
            await pilot.press("x")
            await until(pilot, lambda: isinstance(app.screen, ConfirmDialog))
            app.screen.query_one("#confirm").press()
            await _settle(pilot)
            assert app.screen.query_one("#btn-sync-stop", Button).disabled
            app.active_sync.stop()
    asyncio.run(go())


# ── 7. the quit dialog names a dry run that is waiting for an answer ────────

def test_quit_names_a_dry_run_waiting_for_an_answer(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors

    async def check(app, pilot):
        await pilot.press("r")
        await _until(pilot, lambda: app.active_transfer is not None
                     and app.active_transfer.awaiting_decision)
        await pilot.press("q")
        await _settle(pilot)
        assert isinstance(app.screen, ConfirmDialog)
        message = str(app.screen.query_one("#confirm-message").render())
        assert "waiting" in message and "nothing is published" in message, message
        await pilot.press("escape")
        await _settle(pilot)
        app.active_transfer.decide(False)
        await _finished(pilot, app.active_transfer)
    _run(cfg, mirror_dir.parent, check)
