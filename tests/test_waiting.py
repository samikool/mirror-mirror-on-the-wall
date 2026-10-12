"""`settle`: wait until the app has no work in flight, not a fixed count."""
import asyncio
import threading
import time

from textual import work
from textual.app import App, ComposeResult
from textual.widgets import Label

from waiting import settle


class _Slow(App):
    def compose(self) -> ComposeResult:
        yield Label("", id="out")

    @work(thread=True)
    def slow(self, seconds: float) -> None:
        time.sleep(seconds)
        self.call_from_thread(self.query_one("#out", Label).update, "done")


def test_settle_waits_for_a_thread_worker_and_its_result():
    async def go():
        app = _Slow()
        async with app.run_test() as pilot:
            app.slow(0.4)                # far longer than a handful of pauses
            await settle(pilot)
            assert str(app.query_one("#out", Label).content) == "done"
    asyncio.run(go())


def test_settle_returns_promptly_when_idle():
    async def go():
        app = _Slow()
        async with app.run_test() as pilot:
            t = time.monotonic()
            await settle(pilot)
            assert time.monotonic() - t < 1.0
    asyncio.run(go())


def test_settle_gives_up_on_a_worker_that_never_ends(monkeypatch):
    monkeypatch.delenv("MMW_SETTLE_STRICT", raising=False)
    stop = threading.Event()

    class _Forever(_Slow):
        @work(thread=True)
        def forever(self) -> None:
            stop.wait(10)

    async def go():
        app = _Forever()
        async with app.run_test() as pilot:
            app.forever()
            t = time.monotonic()
            await settle(pilot, timeout=0.5)       # returns, does not raise
            assert time.monotonic() - t < 2.0
            stop.set()
    asyncio.run(go())


def test_an_async_worker_waiting_on_a_dialog_is_not_busy():
    """Flows run as async workers parked on push_screen_wait; that is idle."""
    from textual.screen import Screen

    class _Modal(Screen):
        pass

    async def go():
        app = _Slow()
        async with app.run_test() as pilot:
            async def flow():
                await app.push_screen_wait(_Modal())
            app.run_worker(flow())
            t = time.monotonic()
            await settle(pilot)
            assert time.monotonic() - t < 2.0
    asyncio.run(go())


def test_settle_waits_for_to_thread_work_inside_an_async_worker():
    async def go():
        app = _Slow()
        async with app.run_test() as pilot:
            async def flow():
                await asyncio.to_thread(time.sleep, 0.4)
                app.query_one("#out", Label).update("done")
            app.run_worker(flow())
            await settle(pilot)
            assert str(app.query_one("#out", Label).content) == "done"
    asyncio.run(go())
