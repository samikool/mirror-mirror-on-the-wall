"""Waiting in TUI tests: for a condition, against a clock.

A fixed number of `pilot.pause()`s — or `tries x 0.02 s` — is a guess about
how fast the machine is, and the full suite runs in parallel: under that load
a background thread can take whole seconds to start, so guesses that hold
alone fail a few times per run. Wait for the thing itself, with a generous
deadline that only a real hang reaches.
"""
import time


async def until(pilot, cond, timeout: float = 30.0, step: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition never became true")
        await pilot.pause(step)
