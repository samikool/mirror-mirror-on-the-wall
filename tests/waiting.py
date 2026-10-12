"""Waiting in TUI tests: for a condition, against a clock.

A fixed number of `pilot.pause()`s — or `tries x 0.02 s` — is a guess about
how fast the machine is, and the full suite runs in parallel: under that load
a background thread can take whole seconds to start, so guesses that hold
alone fail a few times per run. Wait for the thing itself, with a generous
deadline that only a real hang reaches.
"""
import asyncio
import os
import time


async def until(pilot, cond, timeout: float = 30.0, step: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        await pilot.pause(step)


_BUSY = ("PENDING", "RUNNING")


def _executor_busy() -> bool:
    """Is the event loop's default thread pool (where `asyncio.to_thread`
    runs) doing anything? Private ThreadPoolExecutor state — test-only."""
    ex = getattr(asyncio.get_running_loop(), "_default_executor", None)
    if ex is None:
        return False
    if not ex._work_queue.empty():
        return True
    return len(ex._threads) > ex._idle_semaphore._value


def _busy(app) -> bool:
    """Thread work in flight: a thread worker, or anything in `to_thread`.
    An async worker is NOT busy by itself — the app's flows are async
    workers parked on `push_screen_wait` for as long as a dialog is open."""
    threads = any(w.state.name in _BUSY and getattr(w, "_thread_worker", False)
                  for w in app.workers)
    return threads or _executor_busy()


async def settle(pilot, timeout: float = 15.0, step: float = 0.02, quiet: int = 3) -> None:
    """Wait until the app has no thread work in flight (see `_busy`) for
    `quiet` consecutive ticks (so a result a finished worker posted back
    with `call_from_thread`, and any worker that result starts, are processed).

    The replacement for "pause N times": that counted *ticks*, so a thread that
    took longer than N ticks to start under a loaded parallel run lost the race.
    A screen that keeps a long-lived worker (a sync session) never goes quiet;
    `settle` then returns at `timeout` rather than failing — the assertion that
    follows decides.

    `MMW_SETTLE_STRICT=1 uv run pytest -n auto` makes a timeout fail instead:
    it lists every test that settles while a thread is deliberately parked —
    such a test should wait for its own condition (`until`) instead.
    """
    deadline = time.monotonic() + timeout
    calm = 0
    while calm < quiet and time.monotonic() < deadline:
        await pilot.pause(step)
        calm = 0 if _busy(pilot.app) else calm + 1
    if calm < quiet and os.environ.get("MMW_SETTLE_STRICT"):
        raise AssertionError("SETTLE-TIMEOUT")
