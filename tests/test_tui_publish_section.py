"""Publish inside MmwApp: start from Networks, browse while it runs, see the
outcome on the Publish section. Real pool, real publish/bundle into tmp."""
import asyncio
import threading

import pytest
from textual.widgets import ListView

from mmw.core import config
from mmw.tui.app import MmwApp
from mmw.tui.transfer import TransferView
from mmw.tui.ui.dialog import ConfirmDialog

from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture
from pickers import confirm_picker, press_bundle, press_publish
from waiting import settle, until


async def _settle(pilot, n=6):
    """Until no work is in flight — see tests/waiting.py. The count is ignored."""
    await settle(pilot)


async def _until(pilot, cond, tries=None):
    """`tries` is ignored: see tests/waiting.py — wait for the thing, against a clock."""
    await until(pilot, cond)

def _run(cfg, cfg_dir, check, **net_changes):
    if net_changes:
        cfg = cfg.model_copy(update={"networks": [
            cfg.networks[0].model_copy(update=net_changes)]})
    config.save(cfg, cfg_dir)

    async def go():
        app = MmwApp(cfg, cfg_dir, initial_section="networks")
        app.notes = []
        app.notify = lambda m, **_: app.notes.append(m)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#net-list", ListView).focus()
            await check(app, pilot)
            if app.active_transfer is not None and app.active_transfer.running:
                app.active_transfer.stop()
                await _until(pilot, lambda: not app.active_transfer.running)
    asyncio.run(go())


def _handled(view) -> bool:
    """The section has handled the view's `Completed`: its status shows the
    view's summary (set in the same handler as the buttons and the toast).
    `_done_displayed` alone is set *before* Completed is even posted."""
    return (view._done_displayed and view.screen is not None
            and str(view.screen.query_one("#page-status").render()) == view.summary())


async def _finished(pilot, view):
    """Done, and the section has handled `Completed` (status, buttons, toast)."""
    await _until(pilot, lambda: _handled(view))
    await _settle(pilot)


def _status(app):
    return str(app.screen.query_one("#page-status").render())


def test_p_publishes_in_the_publish_section(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors

    async def check(app, pilot):
        await press_publish(app, pilot)
        await _until(pilot, lambda: app.current_mode == "publish"
                     and app.screen.query(TransferView))
        view = app.screen.query_one(TransferView)
        await _finished(pilot, view)
        assert view.outcome == "ok"
        assert _status(app).startswith("Published oak")
        assert any("Published oak" in n for n in app.notes)
    _run(cfg, mirror_dir.parent, check)
    assert (net.root / "example.com" / "a" / "shared.deb").exists()


def test_r_shows_the_estimate_then_publish_now_publishes(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors

    async def check(app, pilot):
        await pilot.press("r")
        await _until(pilot, lambda: app.current_mode == "publish"
                     and app.screen.query("#dry-run-report"))
        assert "1.5 KB" in str(app.screen.query_one("#dry-run-report .totals").render())
        app.screen.query_one("#btn-transfer-go").press()
        view = app.screen.query_one(TransferView)
        await _finished(pilot, view)
        assert view.outcome == "ok"
    _run(cfg, mirror_dir.parent, check)
    assert (net.root / "example.com" / "b" / "b.deb").exists()


def test_r_then_discard_publishes_nothing(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors

    async def check(app, pilot):
        await pilot.press("r")
        await _until(pilot, lambda: app.current_mode == "publish"
                     and app.screen.query("#dry-run-report"))
        app.screen.query_one("#btn-transfer-discard").press()
        view = app.screen.query_one(TransferView)
        await _finished(pilot, view)
        assert view.outcome == "note" and "nothing published" in _status(app)
    _run(cfg, mirror_dir.parent, check)
    assert not net.root.exists()


def test_b_asks_from_and_to_on_networks_then_bundles(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    out = mirror_dir.parent / "usb"

    async def check(app, pilot):
        await pilot.press("b")
        await confirm_picker(app, pilot, "BundlePickPage")
        assert app.current_mode == "networks"                 # asked where you are
        await _until(pilot, lambda: app.current_mode == "publish"
                     and app.screen.query(TransferView))
        view = app.screen.query_one(TransferView)
        await _finished(pilot, view)
        assert view.outcome == "ok" and _status(app).startswith("Bundled oak")
    _run(cfg, mirror_dir.parent, check, bundle_dir=out)
    assert list(out.glob("*.tar*"))


def test_incomplete_mirrors_end_it_without_publishing(two_mirrors, monkeypatch):  # noqa: F811
    import mmw.core.verify as verify
    cfg, net, mirror_dir = two_mirrors
    names = [m.name for m in cfg.mirrors]
    # One complete, one not: the popup lists every checked mirror, broken first.
    monkeypatch.setattr(verify, "verify_mirror", lambda m, d, p, **_: verify.VerifyResult(
        m.name, m.provider, total=3, present=3 if m.name == names[0] else 2,
        missing_count=0 if m.name == names[0] else 1))

    async def check(app, pilot):
        await press_publish(app, pilot)
        await _until(pilot, lambda: app.current_mode == "publish"
                     and app.screen.query(TransferView))
        view = app.screen.query_one(TransferView)
        await _finished(pilot, view)
        await _until(pilot, lambda: isinstance(app.screen, ConfirmDialog)
                        and bool(app.screen.query("#confirm")))  # mounted, not just pushed
        listed = str(app.screen.query_one("#confirm-details Label").render())
        assert listed.index(f"✗ {names[1]}") < listed.index(f"✓ {names[0]}")
        assert "1 of 2 mirrors incomplete" in str(
            app.screen.query_one("#confirm-message").render())
        await pilot.press("escape")                          # cancel: publish nothing
        await _until(pilot, lambda: not isinstance(app.screen, ConfirmDialog))
        assert view.outcome == "note" and "incomplete" in _status(app)
    _run(cfg, mirror_dir.parent, check)
    assert not net.root.exists()


@pytest.fixture
def slow_publish(monkeypatch):
    """publish_network that waits until released (or stopped)."""
    import mmw.core.publisher as publisher
    release = threading.Event()

    def slow(net, cfg, mirror_dir, record, stop_event, notify_mirror_start, **_kw):
        while not (release.is_set() or stop_event.is_set()):
            stop_event.wait(0.02)
        from mmw.core.publisher import NetworkPublishResult
        return NetworkPublishResult(network_name=net.name)
    monkeypatch.setattr(publisher, "publish_network", slow)
    return release


def test_browsing_keeps_it_running_and_the_sidebar_shows_it(two_mirrors, slow_publish):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors

    async def check(app, pilot):
        await press_publish(app, pilot)
        await _until(pilot, lambda: app.active_transfer is not None
                     and app.active_transfer._transferring)
        await pilot.press("1")
        await _until(pilot, lambda: app.current_mode == "repositories")
        assert app.active_transfer.running
        await pilot.pause(0.6)
        assert "4 Publish ▸" in str(app.screen.query_one("#nav-publish").render())
        # A second transfer is refused, not stacked.
        await app.start_transfer("publish", net)
        assert any("Already working on oak" in n for n in app.notes)
        slow_publish.set()
        await _until(pilot, lambda: not app.active_transfer.running)
        await pilot.pause(0.6)
        assert str(app.screen.query_one("#nav-publish").render()) == "4 Publish"
    _run(cfg, mirror_dir.parent, check)


def test_quit_while_publishing_asks_and_no_keeps_it(two_mirrors, slow_publish):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors

    async def check(app, pilot):
        await press_publish(app, pilot)
        await _until(pilot, lambda: app.active_transfer is not None
                     and app.active_transfer._transferring)
        await pilot.press("q")
        await _until(pilot, lambda: isinstance(app.screen, ConfirmDialog)
                        and bool(app.screen.query("#confirm")))  # mounted, not just pushed
        await pilot.press("escape")
        await _until(pilot, lambda: not isinstance(app.screen, ConfirmDialog))
        assert app.is_running and app.active_transfer.running
        slow_publish.set()
        await _until(pilot, lambda: not app.active_transfer.running)
    _run(cfg, mirror_dir.parent, check)


# ── review fixes ─────────────────────────────────────────────────────────────

async def _wait_view(app, pilot):
    await _until(pilot, lambda: app.active_transfer is not None)
    return app.active_transfer


def test_a_failure_while_browsing_elsewhere_is_toasted(two_mirrors, monkeypatch):  # noqa: F811
    import mmw.core.publisher as publisher
    cfg, net, mirror_dir = two_mirrors

    def boom(*a, **k):
        raise PermissionError("destination is read-only")
    monkeypatch.setattr(publisher, "publish_network", boom)

    async def check(app, pilot):
        await press_publish(app, pilot)
        view = await _wait_view(app, pilot)
        await app.switch_mode("repositories")
        await _until(pilot, lambda: _handled(view))
        assert view.outcome == "error"
        assert any("read-only" in n for n in app.notes), app.notes
    _run(cfg, mirror_dir.parent, check)


def test_incomplete_mirrors_are_toasted_and_escaped(two_mirrors, monkeypatch):  # noqa: F811
    """A verify error carrying `[/…` used to crash the 60 Hz tick (MarkupError)."""
    import mmw.core.verify as verify
    cfg, net, mirror_dir = two_mirrors
    monkeypatch.setattr(verify, "verify_mirror", lambda m, d, p, **_: verify.VerifyResult(
        m.name, m.provider, error="cannot read [/srv/x] index"))

    async def check(app, pilot):
        await press_publish(app, pilot)
        view = await _wait_view(app, pilot)
        await _finished(pilot, view)
        assert view.outcome == "note" and app.is_running
        assert any("incomplete" in n for n in app.notes)
    _run(cfg, mirror_dir.parent, check)


def test_a_dry_run_waiting_for_you_is_toasted_when_you_are_elsewhere(two_mirrors, monkeypatch):  # noqa: F811
    import mmw.core.verify as verify
    cfg, net, mirror_dir = two_mirrors
    gate = threading.Event()

    def held_verify(m, d, p, **_):
        gate.wait(5)
        return verify.VerifyResult(m.name, m.provider, total=1, present=1)
    monkeypatch.setattr(verify, "verify_mirror", held_verify)

    async def check(app, pilot):
        await pilot.press("r")
        view = await _wait_view(app, pilot)
        await app.switch_mode("repositories")
        await _settle(pilot)
        gate.set()                       # the estimate lands while you are away
        await _until(pilot, lambda: view.awaiting_decision)
        await _settle(pilot)
        assert any("ready" in n and "oak" in n for n in app.notes), app.notes
        await pilot.pause(0.6)
        assert "4 Publish ▸ ?" in str(app.screen.query_one("#nav-publish").render())
        view.decide(False)
        await _finished(pilot, view)
    _run(cfg, mirror_dir.parent, check)


def test_finished_with_errors_never_reads_as_success(two_mirrors, monkeypatch):  # noqa: F811
    import mmw.core.publisher.bundle as bundle_mod
    from mmw.core.publisher.results import BundleResult
    cfg, net, mirror_dir = two_mirrors
    out = mirror_dir.parent / "usb"
    monkeypatch.setattr(bundle_mod, "bundle_network", lambda net, *a, **k: BundleResult(
        network_name=net.name, out_path=out / "x.tar", sent=3,
        errors=["a blob vanished mid-archive"]))

    async def check(app, pilot):
        await press_bundle(app, pilot)
        view = await _wait_view(app, pilot)
        await _finished(pilot, view)
        title = str(view.query_one("#bun-title-bar").render())
        assert "✓" not in title and "1 error" in title
        status = app.screen.query_one("#page-status")
        assert not status.has_class("-ok")
        assert not str(status.render()).startswith("Bundled")
    _run(cfg, mirror_dir.parent, check, bundle_dir=out)


def test_stop_while_preparing_stops_the_remaining_checks(two_mirrors, monkeypatch):  # noqa: F811
    import mmw.core.verify as verify
    cfg, net, mirror_dir = two_mirrors
    seen, gate = [], threading.Event()

    def slow_verify(m, d, p, **_):
        seen.append(m.name)
        gate.wait(5)
        return verify.VerifyResult(m.name, m.provider, total=1, present=1)
    monkeypatch.setattr(verify, "verify_mirror", slow_verify)
    import mmw.tui.publish_flows as flows_mod
    monkeypatch.setattr(flows_mod, "_PREPARE_WORKERS", 1)        # one at a time

    async def check(app, pilot):
        await press_publish(app, pilot)
        view = await _wait_view(app, pilot)
        await _until(pilot, lambda: seen)
        view.stop()
        gate.set()
        await _finished(pilot, view)
        assert view.outcome == "stopped"
        assert seen == [seen[0]], "the second mirror was still verified after Stop"
    _run(cfg, mirror_dir.parent, check)
    assert not net.root.exists()


def test_stop_after_publish_now_publishes_nothing(two_mirrors, monkeypatch):  # noqa: F811
    import mmw.core.publisher as publisher
    cfg, net, mirror_dir = two_mirrors
    called = []
    monkeypatch.setattr(publisher, "publish_network", lambda *a, **k: called.append(1))

    async def check(app, pilot):
        await pilot.press("r")
        view = await _wait_view(app, pilot)
        await _until(pilot, lambda: view.awaiting_decision)
        view.decide(True)
        view.stop()                    # before the post-confirm count finishes
        await _finished(pilot, view)
        assert view.outcome == "stopped" and called == []
    _run(cfg, mirror_dir.parent, check)


def test_quit_dialog_is_honest_about_a_bundle_writing_its_archive():
    import inspect

    from mmw.tui.ui import base
    assert "archive" in inspect.getsource(base.ThemedApp.action_quit)


def test_a_long_check_shows_its_step_beside_the_count(two_mirrors, monkeypatch):  # noqa: F811
    """A VS Code unit is one "mirror" and most of the verify: its steps show
    on the label while the count sits still, and go when the checks end."""
    import mmw.core.verify as verify
    cfg, net, mirror_dir = two_mirrors
    gate = threading.Event()

    def stepping_verify(m, d, p, say=None):
        say(f"reading 3 new descriptions ({m.name})…")
        gate.wait(5)
        return verify.VerifyResult(m.name, m.provider, total=1, present=1)
    monkeypatch.setattr(verify, "verify_mirror", stepping_verify)

    async def check(app, pilot):
        await press_publish(app, pilot)
        view = await _wait_view(app, pilot)
        label = lambda: str(view.query_one(f"#{view.ID_PREFIX}-mirror-label").render())  # noqa: E731
        await _until(pilot, lambda: "reading 3 new descriptions" in label())
        assert label().startswith("  Verifying mirrors  ·  ")
        log = view.query_one(f"#{view.ID_PREFIX}-log")
        assert not any("reading 3 new" in line.text for line in log.lines), \
            "steps belong on the label, not in the log"
        gate.set()
        await _finished(pilot, view)
        assert "reading 3 new descriptions" not in label()
    _run(cfg, mirror_dir.parent, check)
