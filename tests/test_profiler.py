"""The in-process sampling profiler.

Written because `py-spy` and `gdb` both need ptrace, and this machine runs
`kernel.yama.ptrace_scope = 1` — three attempts to diagnose a busy thread ended
in inference rather than a stack. The profiler has to work from inside.
"""
import threading
import time

from mmw.core.profiler import SamplingProfiler, _cpu_deltas


def _spin(stop: threading.Event, used: list | None = None) -> None:
    """Burn CPU until stopped; `used[0]` is this thread's CPU time so far."""
    x = 0
    while not stop.is_set():
        for i in range(20_000):
            x += i * i
        if used is not None:
            used[0] = time.thread_time()


def _wait(cond, timeout: float = 30.0) -> None:
    """Until `cond()` — work done, not wall time: a fixed sleep is a guess
    about how much CPU a thread gets, and a loaded parallel run starves it."""
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condition never became true"
        time.sleep(0.01)


def _hot(prof, stop, name="HOT", cpu: float = 0.3, samples: int = 30):
    """Start a spinning thread and return once it has burned `cpu` seconds
    and the profiler has taken `samples` samples."""
    used = [0.0]
    t = threading.Thread(target=_spin, args=(stop, used), name=name, daemon=True)
    t.start()
    _wait(lambda: used[0] >= cpu and prof._samples >= samples)
    return t


def _block_forever() -> None:
    lock = threading.Lock()
    lock.acquire()
    lock.acquire()          # never returns; has a stack, burns no CPU


def test_cpu_is_credited_to_the_busy_thread_not_the_blocked_one():
    """The distinction that makes this useful. `sys._current_frames()` alone
    reports a thread blocked on a lock as though it were working — every
    blocked worker has a perfectly good stack."""
    stop = threading.Event()
    prof = SamplingProfiler(interval=0.005)
    prof.start()
    threading.Thread(target=_block_forever, name="BLOCKED", daemon=True).start()
    _hot(prof, stop)
    stop.set()
    prof.stop()

    report = prof.report()
    assert "HOT" in report
    hot_line = [ln for ln in report.splitlines() if ln.strip().startswith("HOT")]
    assert hot_line, report
    blocked_line = [ln for ln in report.splitlines()
                    if ln.strip().startswith("BLOCKED") and "% of process CPU" in ln]
    assert not blocked_line, f"a blocked thread was reported as burning CPU:\n{report}"


def test_the_report_names_the_function_that_was_running():
    stop = threading.Event()
    prof = SamplingProfiler(interval=0.005)
    prof.start()
    _hot(prof, stop)
    stop.set()
    prof.stop()

    assert "_spin" in prof.report(), prof.report()


def test_a_thread_that_exits_early_is_still_accounted_for():
    """CPU used to be read only at start and stop, so a worker that finished in
    between vanished from /proc and was credited with nothing. Executor threads
    are recycled, so that is the common case, not the edge case."""
    stop = threading.Event()
    prof = SamplingProfiler(interval=0.005)
    prof.start()
    t = _hot(prof, stop, name="SHORTLIVED")
    stop.set()
    t.join(timeout=30)
    n = prof._samples
    _wait(lambda: prof._samples >= n + 20)   # sampled again after it left /proc
    prof.stop()

    assert "SHORTLIVED" in prof.report(), prof.report()


def test_stopping_without_starting_is_harmless():
    SamplingProfiler().stop()


def test_the_profiler_survives_a_missing_proc(monkeypatch):
    """A profiler must never take the process down. Degrade the report, not
    the run."""
    import mmw.core.profiler as mod

    monkeypatch.setattr(mod, "_thread_cpu_ticks", lambda: {})
    prof = SamplingProfiler(interval=0.005)
    prof.start()
    time.sleep(0.2)
    prof.stop()
    assert "mmw sampling profile" in prof.report()


def test_cpu_deltas_ignore_threads_that_did_nothing():
    assert _cpu_deltas({1: 100, 2: 50}, {1: 160, 2: 50}) == {1: 60}


def test_the_report_writes_to_a_file(tmp_path):
    prof = SamplingProfiler(interval=0.01)
    prof.start()
    time.sleep(0.15)
    prof.stop()
    out = prof.write(tmp_path / "p.txt")
    assert out.exists() and "sampling profile" in out.read_text()


def test_profiling_is_off_by_default():
    """`--profile` is opt-in: the contextmanager must be a no-op otherwise, so
    callers need no second code path."""
    from mmw.cli import _profiling

    with _profiling(False) as path:
        assert path is None


def test_interim_snapshots_are_written_during_a_long_run(tmp_path):
    """A multi-hour sync must not lose its profile to a crash, and the interim
    files show the picture changing between phases rather than only the average
    over the whole run."""
    out = tmp_path / "p.txt"
    stop = threading.Event()
    prof = SamplingProfiler(interval=0.005, snapshot=out, snapshot_every=0.3)
    prof.start()
    _hot(prof, stop)
    _wait(lambda: out.exists() and "HOT" in out.read_text())   # an interim report while still running
    stop.set()
    prof.stop()


def test_a_snapshot_is_never_half_written(tmp_path):
    """Written via tmp+rename: a reader tailing the file during a long run must
    never see a truncated report."""
    out = tmp_path / "p.txt"
    prof = SamplingProfiler(interval=0.005, snapshot=out, snapshot_every=0.2)
    prof.start()
    time.sleep(0.6)
    prof.stop()
    assert not out.with_suffix(".tmp").exists(), "left a partial file behind"
    assert out.read_text().startswith("mmw sampling profile")


def test_snapshots_are_off_unless_asked_for(tmp_path):
    prof = SamplingProfiler(interval=0.005)
    prof.start()
    time.sleep(0.2)
    prof.stop()
    assert not list(tmp_path.iterdir())
