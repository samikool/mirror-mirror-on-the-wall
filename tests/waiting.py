"""Waiting in TUI tests: for a condition, against a clock.

A fixed number of `pilot.pause()`s — or `tries x 0.02 s` — is a guess about
how fast the machine is, and the full suite runs in parallel: under that load
a background thread can take whole seconds to start, so guesses that hold
alone fail a few times per run. Wait for the thing itself, with a generous
deadline that only a real hang reaches.
"""
import asyncio
import os
import sys
import time


async def until(pilot, cond, timeout: float = 30.0, step: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        await pilot.pause(step)


async def shows(pilot, read, *expected: str, absent: tuple[str, ...] = (),
                timeout: float = 30.0, step: float = 0.02) -> str:
    """Wait until `read()` contains every `expected` piece of text (and none
    of `absent`); return it. Fails with the last text it read.

    Wait for the exact thing the test checks, not a piece of it: waiting for
    `"macos" in status` and then asserting the full macos line raced the
    progress line `Checking 3.12-macos-amd64 (2/2)…`, which also says macos.
    """
    deadline = time.monotonic() + timeout
    while True:
        text = str(read())
        if all(e in text for e in expected) and not any(a in text for a in absent):
            return text
        if time.monotonic() > deadline:
            missing = [e for e in expected if e not in text]
            present = [a for a in absent if a in text]
            raise AssertionError(f"never showed {missing}" + (f", still showed {present}" if present else "")
                                 + f"; last read:\n{text}")
        await pilot.pause(step)


async def at_step(pilot, wizard, step: int, timeout: float = 30.0) -> None:
    """Wait until `wizard` is on `step` AND takes Next again. Right after a
    move it drops Next until a queued callback has run (`_accepting_next`:
    a double-click must not skip Confirm), so a press made the moment `step`
    changed could be dropped as that double-click, and the test then waited
    out its timeout for a screen that was never coming."""
    await until(pilot, lambda: wizard.step == step and wizard._accepting_next, timeout)


_BUSY = ("PENDING", "RUNNING")


def _executor_busy() -> bool:
    """Is the event loop's default thread pool (where `asyncio.to_thread`
    runs) doing anything? Private ThreadPoolExecutor state — test-only."""
    ex = getattr(asyncio.get_running_loop(), "_default_executor", None)
    if ex is None:
        return False
    if not ex._work_queue.empty():
        return True
    # A pool thread is busy while a work item is on its stack. (The pool's
    # idle semaphore is not a count of idle threads: it read 1 with both
    # threads parked in `queue.get`, and settle sat out its whole timeout.)
    frames = sys._current_frames()
    return any(_running_work(frames.get(t.ident)) for t in list(ex._threads))


def _running_work(frame) -> bool:
    while frame is not None:
        code = frame.f_code
        if code.co_name == "run" and code.co_filename.endswith(_FUTURES_THREAD):
            return True
        frame = frame.f_back
    return False


_FUTURES_THREAD = os.path.join("concurrent", "futures", "thread.py")


def _busy(app) -> bool:
    """Thread work in flight: a thread worker, or anything in `to_thread`.
    An async worker is NOT busy by itself — the app's flows are async
    workers parked on `push_screen_wait` for as long as a dialog is open."""
    threads = any(w.state.name in _BUSY and getattr(w, "_thread_worker", False)
                  for w in app.workers)
    return threads or _executor_busy()


def _screens(app) -> tuple:
    """The screen stack's identity — changes on every push, pop and switch."""
    return tuple(id(s) for s in app.screen_stack)


def _pending(app) -> bool:
    """Messages queued for the app or its current screen (private Textual
    state — test-only): a push still mounting, a result still to handle."""
    return any(getattr(n, "_message_queue", None) is not None and not n._message_queue.empty()
               for n in (app, app.screen))


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
    seen = None
    while calm < quiet and time.monotonic() < deadline:
        await pilot.pause(step)
        # Quiet means the screens are quiet too: a push or pop is not thread
        # work, and a test that read `app.screen` right after settling could
        # catch the screen before the one its key press was opening.
        now = _screens(pilot.app)
        calm = 0 if (_busy(pilot.app) or now != seen or _pending(pilot.app)) else calm + 1
        seen = now
    if calm < quiet and os.environ.get("MMW_SETTLE_STRICT"):
        raise AssertionError("SETTLE-TIMEOUT")
