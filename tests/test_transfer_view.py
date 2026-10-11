"""TransferView's own lifecycle: preparing → transferring → one outcome,
posted as `Completed`; a dry run's decision; the app's active transfer."""
import asyncio
from types import SimpleNamespace

from mmw.tui.transfer import BUNDLE, PUBLISH, TransferView
from mmw.tui.ui.base import ThemedApp


def _run(check, kind=PUBLISH):
    async def go():
        class _App(ThemedApp):
            def compose(self):
                yield TransferView(kind, "lab")

            def on_transfer_view_completed(self, event):
                self.completed.append(event.view.outcome)

        app = _App()
        app.completed = []
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await check(app, app.query_one(TransferView), pilot)
    asyncio.run(go())


async def _ticks(pilot, n=4):
    for _ in range(n):
        await pilot.pause(0.03)


def _title(view):
    return str(view.query_one(f"#{view.ID_PREFIX}-title-bar").render())


def test_it_registers_as_the_apps_transfer_and_reports_percent():
    async def check(app, view, pilot):
        assert app.active_transfer is view and view.running
        assert "Preparing" in _title(view) and view.progress() is None
        view.begin(4, {"m": 4})
        view.record("h/r/pool/a.deb", "linked")
        assert view.progress() == 25
        for i in range(9):                       # the count was an estimate
            view.record(f"h/r/pool/{i}.deb", "skipped")
        assert view.progress() == 100
        await _ticks(pilot)
        assert "Publishing" in _title(view)
    _run(check)


def test_done_posts_completed_once_with_the_outcome():
    async def check(app, view, pilot):
        view.begin(1)
        view.mark_done(SimpleNamespace(linked=1, skipped=0, errors=[]))
        await _ticks(pilot)
        await _ticks(pilot)
        assert app.completed == ["ok"] and not view.running
        assert "✓ Published" in _title(view)
        assert view.summary() == "Published lab — 1 linked, 0 already current."
    _run(check)


def test_stop_says_stopped_never_complete():
    async def check(app, view, pilot):
        view.begin(10)
        view.stop()
        assert "Stopping" in _title(view)
        view.mark_done(SimpleNamespace(sent=3, errors=[]))
        await _ticks(pilot)
        assert app.completed == ["stopped"]
        assert "Stopped" in _title(view) and view.summary().startswith("Bundling lab stopped")
    _run(check, BUNDLE)


def test_an_error_is_shown_in_the_log_and_the_title():
    async def check(app, view, pilot):
        view.mark_error("disk full")
        await _ticks(pilot)
        assert app.completed == ["error"] and "Error" in _title(view)
        assert view.summary() == "lab: disk full"
    _run(check)


def test_a_note_ends_it_without_calling_it_published():
    async def check(app, view, pilot):
        view.set_phase("Verifying mirrors", 2)
        view.mirror_done(1, "m1", "2 of 9 missing", False)
        view.finish_note("incomplete mirrors — nothing published", ["  • m1: 2 of 9 missing"])
        await _ticks(pilot)
        assert app.completed == ["note"]
        assert "Not published" in _title(view)
    _run(check)


def test_a_dry_run_waits_for_the_decision():
    async def check(app, view, pilot):
        result = SimpleNamespace(mirrors=[], total_files=0, total_bytes=0, skipped_bytes=0,
                                 linked_bytes=0, errors=[], dist_upgrade_paths=[],
                                 dist_upgrade_warnings=[])
        task = asyncio.ensure_future(view.ask_publish(result))
        await _ticks(pilot)
        assert view.awaiting_decision and view.query("#dry-run-report")
        view.decide(True)
        assert await task is True
        await _ticks(pilot)
        assert not view.query("#dry-run-report")
    _run(check)


def test_stopping_while_a_dry_run_waits_answers_no():
    async def check(app, view, pilot):
        result = SimpleNamespace(mirrors=[], total_files=0, total_bytes=0, skipped_bytes=0,
                                 linked_bytes=0, errors=[], dist_upgrade_paths=[],
                                 dist_upgrade_warnings=[])
        task = asyncio.ensure_future(view.ask_publish(result))
        await _ticks(pilot)
        view.stop()
        assert await task is False
    _run(check)
