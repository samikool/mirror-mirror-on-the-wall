"""In-process sampling profiler — answers "what is that thread doing?".

Built because the obvious tools could not be used here: `py-spy` and `gdb` both
need `ptrace`, and this machine runs `kernel.yama.ptrace_scope = 1`, so nothing
outside the process's own descendants can attach. Three separate attempts to
diagnose a busy main thread ended in inference rather than a stack.

Two signals, combined, because neither alone is enough:

* **Per-thread CPU from `/proc`** — authoritative for *who* is burning CPU.
  `sys._current_frames()` alone cannot tell you this: a thread blocked on a lock
  still has a perfectly good stack, so a naive stack histogram reports blocked
  threads as busy ones.
* **Stack samples** — what that thread is executing. The leaf frame is
  self-time; the whole stack gives the caller chain.

Sampling is deliberately cheap: one `sys._current_frames()` call per tick, which
briefly takes the GIL, and one small read per thread from `/proc`.
"""
import collections
import os
import sys
import threading
import time
from pathlib import Path

# Frames in these files are runtime plumbing; the interesting answer is almost
# always the nearest frame *outside* them.
_FRAMEWORK_HINTS = ("/textual/", "/rich/", "/asyncio/", "/httpx/", "/httpcore/")


class SamplingProfiler:
    """Samples every thread's stack and CPU use until stopped."""

    def __init__(self, interval: float = 0.01, depth: int = 40,
                 snapshot: "Path | None" = None, snapshot_every: float = 600.0):
        """snapshot: write an interim report here every `snapshot_every`
        seconds. A multi-hour run must not lose everything to a crash or a
        SIGKILL, and the interim files also show how the picture changes between
        phases — metadata, downloads, reconcile."""
        self.interval = interval
        self.depth = depth
        self._snapshot = snapshot
        self._snapshot_every = snapshot_every
        self._last_snapshot = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # leaf frame -> count, per thread name
        self._leaves: dict[str, collections.Counter] = collections.defaultdict(
            collections.Counter)
        self._stacks: dict[str, collections.Counter] = collections.defaultdict(
            collections.Counter)
        self._samples = 0
        self._started = 0.0
        self._elapsed = 0.0
        # First and last CPU reading seen for each OS thread. Sampled *during*
        # the run, not just at the ends: a worker that exits before `stop()`
        # disappears from /proc, and reading only at the boundaries credited it
        # with nothing. Executor threads are recycled, so this is the common case.
        self._cpu_first: dict[int, int] = {}
        self._cpu_last: dict[int, int] = {}
        self._names: dict[int, str] = {}
        # ~20 Hz. Frequent enough that a thread living under a second is
        # observed at least twice — one observation gives first == last and a
        # delta of zero, which silently drops it from the report. Reading a few
        # dozen small procfs files at this rate is microseconds.
        self._cpu_every = max(1, int(0.05 / max(interval, 0.001)))
        self._tick = 0

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._started = time.monotonic()
        self._record_cpu()
        self._thread = threading.Thread(target=self._run, name="mmw-profiler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._record_cpu()
        self._elapsed = time.monotonic() - self._started
        self._stop.set()
        self._thread.join(timeout=2)
        self._thread = None

    def _run(self) -> None:
        me = threading.get_ident()
        while not self._stop.is_set():
            try:
                self._sample(me)
            except Exception:
                pass          # a profiler must never take the process down
            self._maybe_snapshot()
            self._stop.wait(self.interval)

    def _sample(self, me: int) -> None:
        # Map Python idents to OS tids so stacks and /proc CPU line up.
        for t in threading.enumerate():
            if t.ident is not None and t.native_id is not None:
                self._names[t.native_id] = t.name
        idents = {t.ident: t for t in threading.enumerate()}

        for ident, frame in sys._current_frames().items():
            if ident == me:
                continue
            t = idents.get(ident)
            name = t.name if t is not None else f"tid-{ident}"
            stack = []
            f = frame
            for _ in range(self.depth):
                if f is None:
                    break
                stack.append(f"{Path(f.f_code.co_filename).name}:{f.f_code.co_name}")
                f = f.f_back
            if not stack:
                continue
            self._leaves[name][_interesting(frame, stack)] += 1
            self._stacks[name][" <- ".join(stack[:6])] += 1
        self._samples += 1
        self._tick += 1
        if self._tick % self._cpu_every == 0:
            self._record_cpu()

    def _maybe_snapshot(self) -> None:
        if self._snapshot is None:
            return
        now = time.monotonic()
        if now - self._last_snapshot < self._snapshot_every:
            return
        self._last_snapshot = now
        try:
            self._record_cpu()
            self._elapsed = now - self._started
            tmp = self._snapshot.with_suffix(".tmp")
            tmp.write_text(self.report() + "\n")
            tmp.replace(self._snapshot)      # never a half-written report
        except Exception:
            pass                              # a profiler must not break the run

    def _record_cpu(self) -> None:
        """Snapshot per-thread CPU. Called at start, at stop, and periodically
        during the run so a thread that exits early is still accounted for."""
        for tid, ticks in _thread_cpu_ticks().items():
            self._cpu_first.setdefault(tid, ticks)
            self._cpu_last[tid] = ticks

    # ── reporting ────────────────────────────────────────────────────────────

    def report(self, limit: int = 12) -> str:
        cpu = _cpu_deltas(self._cpu_first, self._cpu_last)
        total_cpu = sum(cpu.values()) or 1
        hz = os.sysconf("SC_CLK_TCK")
        out = [
            "mmw sampling profile",
            f"  {self._samples:,} samples over {self._elapsed:.1f}s "
            f"at {1 / self.interval:.0f} Hz",
            "",
            "CPU BY THREAD  (from /proc — this is who actually burned CPU)",
        ]
        by_name: collections.Counter = collections.Counter()
        for tid, ticks in cpu.items():
            by_name[self._names.get(tid, f"tid-{tid}")] += ticks
        for name, ticks in by_name.most_common(10):
            secs = ticks / hz
            out.append(
                f"  {name:<28} {secs:7.1f}s  {ticks / total_cpu * 100:5.1f}% of process CPU"
                f"  ({secs / max(self._elapsed, 0.001) * 100:5.1f}% of a core)"
            )

        for name, _ in by_name.most_common(4):
            leaves = self._leaves.get(name)
            if not leaves:
                continue
            total = sum(leaves.values()) or 1
            out += ["", f"WHERE '{name}' WAS  ({total:,} stack samples)"]
            for fn, n in leaves.most_common(limit):
                out.append(f"  {n / total * 100:5.1f}%  {fn}")
            top = self._stacks[name].most_common(3)
            if top:
                out.append(f"  top stack: {top[0][0]}")
        return "\n".join(out)

    def write(self, path: Path) -> Path:
        path.write_text(self.report() + "\n")
        return path


def _interesting(frame, stack: list[str]) -> str:
    """The nearest frame that is *our* code, else the raw leaf.

    A leaf histogram full of `selectors.py:select` says only "it was in the
    runtime"; the caller one level out is what a reader can act on.
    """
    f = frame
    while f is not None:
        name = f.f_code.co_filename
        if not any(h in name for h in _FRAMEWORK_HINTS):
            return f"{Path(name).name}:{f.f_code.co_name}"
        f = f.f_back
    return stack[0]


def _thread_cpu_ticks() -> dict[int, int]:
    """utime+stime per OS thread, straight from /proc. Linux only; empty
    elsewhere, which degrades the report rather than breaking it."""
    out: dict[int, int] = {}
    try:
        for entry in os.listdir("/proc/self/task"):
            try:
                fields = Path(f"/proc/self/task/{entry}/stat").read_text().rsplit(")", 1)[1].split()
                out[int(entry)] = int(fields[11]) + int(fields[12])
            except (OSError, IndexError, ValueError):
                continue
    except OSError:
        pass
    return out


def _cpu_deltas(first: dict[int, int], last: dict[int, int]) -> dict[int, int]:
    return {tid: last[tid] - first.get(tid, 0)
            for tid in last if last[tid] - first.get(tid, 0) > 0}
