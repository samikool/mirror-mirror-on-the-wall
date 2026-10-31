"""The disk I/O layer's lane contract: item order, streaming, bounded work,
stop, and n == 1 meaning exactly today's inline loop."""
import itertools
import os
import threading
import time

import pytest

from mmw.core import disk_io
from mmw.core.disk_io import DiskJob, DiskStats, Lane, Limits


def _lane(n, **kw):
    return Lane(n, "test", DiskStats(), "read-meta", **kw)


def test_ordered_map_yields_in_item_order():
    lane = _lane(4)
    def fn(i):
        time.sleep((10 - i) * 0.005)
        return i * 2
    assert list(lane.ordered_map(fn, range(10))) == [(i, i * 2) for i in range(10)]
    lane.close()


def test_ordered_map_yields_before_later_items_finish():
    lane = _lane(4)
    gate, timed_out = threading.Event(), []

    def fn(i):
        if i == 19:
            timed_out.append(not gate.wait(5))      # the last item is stuck
        return i

    results = lane.ordered_map(fn, range(20), window=20)
    assert next(results) == (0, 0)                  # reported while item 19 still waits
    gate.set()
    assert [r for _, r in results] == list(range(1, 20))
    assert timed_out == [False]
    lane.close()


def test_ordered_map_window_bounds_in_flight():
    lane = _lane(2)
    lock, started, yielded, peak = threading.Lock(), [0], [0], [0]

    def fn(i):
        with lock:
            started[0] += 1
            peak[0] = max(peak[0], started[0] - yielded[0])
        time.sleep(0.002)
        return i

    for _ in lane.ordered_map(fn, range(40), window=3):
        with lock:
            yielded[0] += 1
    assert peak[0] <= 3
    lane.close()


def test_ordered_map_pulls_input_lazily():
    lane = _lane(2)
    pulled = [0]

    def source():
        for i in itertools.count():
            pulled[0] += 1
            yield i

    gen = lane.ordered_map(lambda i: i, source(), window=4)
    assert [next(gen) for _ in range(3)] == [(0, 0), (1, 1), (2, 2)]
    gen.close()
    assert pulled[0] < 4 + 3
    lane.close()


@pytest.mark.parametrize("n", [1, 4])
def test_ordered_map_exception_is_the_items_result(n):
    lane = _lane(n)

    def fn(i):
        if i == 2:
            raise OSError("boom")
        return i

    got = list(lane.ordered_map(fn, range(5)))
    assert [i for i, _ in got] == list(range(5))
    assert isinstance(got[2][1], OSError)
    assert [r for i, r in got if i != 2] == [0, 1, 3, 4]
    lane.close()


@pytest.mark.parametrize("n", [1, 4])
def test_ordered_map_propagates_base_exceptions(n):
    lane = _lane(n)

    def fn(i):
        if i == 1:
            raise KeyboardInterrupt
        return i

    with pytest.raises(KeyboardInterrupt):
        list(lane.ordered_map(fn, range(10)))
    closer = threading.Thread(target=lane.close)
    closer.start()
    closer.join(2)
    assert not closer.is_alive()


def test_ordered_map_stop_cancels_pending():
    lane = _lane(2)
    stop = threading.Event()
    events = [threading.Event() for _ in range(100)]
    started = []

    def fn(i):
        started.append(i)
        events[i].wait(5)
        return i

    gen = lane.ordered_map(fn, range(100), stop_event=stop, window=8)
    events[0].set()
    assert next(gen) == (0, 0)
    stop.set()
    for e in events:
        e.set()
    rest = [i for i, _ in gen]
    assert rest == list(range(1, 1 + len(rest)))   # a contiguous prefix, in order
    assert len(started) <= 1 + 8
    lane.close()


def test_a_stop_yields_every_item_that_ran():
    """A stop never cancels what was submitted: a worker takes an item off the
    queue a moment before marking it running, so a cancel could land on item
    k while k+1 already ran — on disk behind a gap the caller never settled
    (a stopped publish left files outside its record; CI,
    test_link_many_stop_records_only_settled_rows). What was submitted
    finishes and is yielded; nothing more is started."""
    lane = _lane(2)
    stop, gate = threading.Event(), threading.Event()
    started = []

    def fn(i):
        started.append(i)
        if i in (1, 2):
            gate.wait(5)                        # both workers busy; the rest queued
        return i

    gen = lane.ordered_map(fn, range(100), stop_event=stop, window=8)
    assert next(gen) == (0, 0)
    deadline = time.monotonic() + 5
    while not {1, 2} <= set(started) and time.monotonic() < deadline:
        time.sleep(0.001)
    stop.set()
    gate.set()
    yielded = [0] + [i for i, _ in gen]
    lane.close()
    assert yielded == list(range(len(yielded)))           # a contiguous prefix
    assert sorted(started) == yielded                     # everything that ran, nothing else
    assert len(yielded) <= 1 + 8                          # bounded by the window


def test_a_stop_never_leaves_a_gap_even_with_workers_freed_mid_stop(monkeypatch):
    """The race itself, forced: if a stop cancels anything, the first cancel
    that lands frees both workers and waits for one to take a queued item.
    Cancelling front-first, item 3 was cancelled and 4–7 ran unyielded (this
    test failed every time); any cancel on a stop can open the same gap."""
    import concurrent.futures

    lane = _lane(2)
    stop, gate = threading.Event(), threading.Event()
    started = []

    def fn(i):
        started.append(i)
        if i in (1, 2):
            gate.wait(5)
        return i

    real_cancel = concurrent.futures.Future.cancel
    hooked = []

    def cancel(fut):
        landed = real_cancel(fut)
        if landed and not hooked:
            hooked.append(True)
            before = len(started)
            gate.set()
            deadline = time.monotonic() + 5
            while len(started) == before and time.monotonic() < deadline:
                time.sleep(0.001)
        return landed

    monkeypatch.setattr(concurrent.futures.Future, "cancel", cancel)
    gen = lane.ordered_map(fn, range(8), stop_event=stop, window=8)
    assert next(gen) == (0, 0)
    deadline = time.monotonic() + 5
    while not {1, 2} <= set(started) and time.monotonic() < deadline:
        time.sleep(0.001)
    stop.set()
    gate.set()
    yielded = [0] + [i for i, _ in gen]
    lane.close()
    assert yielded == list(range(len(yielded)))
    assert set(started) <= set(yielded), f"ran but never yielded: {sorted(set(started) - set(yielded))}"


def test_closing_the_generator_cancels_unstarted():
    lane = _lane(2)
    calls = []
    gen = lane.ordered_map(lambda i: calls.append(i) or i, range(100))
    next(gen)
    gen.close()
    lane.close()
    assert len(calls) < 20


def test_n1_runs_inline_without_threads():
    lane = _lane(1)
    main, before, seen = threading.current_thread(), threading.active_count(), []

    def fn(i):
        seen.append(threading.current_thread() is main)
        return i

    assert [r for _, r in lane.ordered_map(fn, range(5))] == list(range(5))
    assert seen == [True] * 5
    assert threading.active_count() == before
    lane.close()


def test_n1_honours_stop_before_each_item():
    lane = _lane(1)
    stop = threading.Event()
    gen = lane.ordered_map(lambda i: i, range(5), stop_event=stop)
    assert next(gen) == (0, 0)
    stop.set()
    assert list(gen) == []


def test_two_jobs_never_share_a_lane(tmp_path):
    paths = []
    for i in range(3):
        p = tmp_path / f"f{i}"
        p.write_bytes(b"x")
        paths.append(p)
    held = threading.Event()
    a = DiskJob("a", limits=Limits(read_threads=2))
    b = DiskJob("b", limits=Limits(read_threads=2))
    blocked = a.read_map(lambda p: held.wait(5), paths * 4)
    stuck = threading.Thread(target=lambda: list(blocked))
    stuck.start()
    t0 = time.monotonic()
    assert [isinstance(r, os.stat_result) for _, r in b.stat(paths)] == [True] * 3
    assert time.monotonic() - t0 < 2
    held.set()
    stuck.join(5)
    a.close()
    b.close()


def test_disk_job_stats_record_route_time(tmp_path):
    paths = [tmp_path / f"f{i}" for i in range(4)]
    for p in paths:
        p.write_bytes(b"abc")
    with DiskJob("t", limits=Limits(read_threads=2)) as job:
        list(job.stat(paths))
        assert job.stats.timings()["disk read-meta"] > 0
        assert job.stats.routes["read-meta"].ops == 4


def test_remove_returns_size_or_none(tmp_path):
    there = tmp_path / "there"
    there.write_bytes(b"12345")
    with DiskJob("t") as job:
        got = dict(job.remove([there, tmp_path / "gone"]))
    assert got == {there: 5, tmp_path / "gone": None}
    assert not there.exists()


def test_place_links_then_skips_same_inode(tmp_path):
    src = tmp_path / "src"
    src.write_bytes(b"blob")
    dst = tmp_path / "pub" / "a" / "dst"
    with DiskJob("t") as job:
        assert list(job.place([(src, dst)], tmp_dir=tmp_path / "tmp")) == [((src, dst), "linked")]
        assert list(job.place([(src, dst)], tmp_dir=tmp_path / "tmp")) == [((src, dst), "skipped")]
    assert os.path.samefile(src, dst)


def test_read_small_reads_bytes_and_keeps_errors(tmp_path):
    a = tmp_path / "a"
    a.write_bytes(b"A")
    with DiskJob("t", limits=Limits(read_threads=4)) as job:
        got = dict(job.read_small([a, tmp_path / "missing"]))
    assert got[a] == b"A"
    assert isinstance(got[tmp_path / "missing"], OSError)


def test_configure_is_pinned_in_tests():
    """The suite runs serial: conftest pins 1 / 1 / 0 so outcome tests stay
    deterministic; a config load must not unpin it."""
    class S:
        disk_write_threads, disk_read_threads, disk_readahead = 8, 16, 16
    disk_io.configure(S())
    assert disk_io.limits() == Limits(1, 1, 0)


def test_read_files_dedups_and_keeps_errors(tmp_path, monkeypatch):
    (tmp_path / "a").write_bytes(b"A")
    seen, real = [], disk_io.ops.read_bytes
    monkeypatch.setattr(disk_io.ops, "read_bytes", lambda p: (seen.append(p), real(p))[1])
    got = disk_io.read_files([tmp_path / "a", tmp_path / "missing", tmp_path / "a"])
    assert got[tmp_path / "a"] == b"A" and isinstance(got[tmp_path / "missing"], OSError)
    assert seen == [tmp_path / "a", tmp_path / "missing"]


def test_read_files_reads_are_in_flight_together(monkeypatch):
    # Four reads that each wait for the other three: one at a time, the
    # barrier breaks instead.
    barrier = threading.Barrier(4, timeout=5)

    def reader(path):
        barrier.wait()
        return str(path).encode()

    monkeypatch.setattr(disk_io.ops, "read_bytes", reader)
    with DiskJob("t", limits=Limits(read_threads=4)) as job:
        got = disk_io.read_files(["w", "x", "y", "z"], job=job)
    assert got == {"w": b"w", "x": b"x", "y": b"y", "z": b"z"}


def test_read_files_reports_progress():
    calls = []
    disk_io.read_files([], on_progress=lambda d, t: calls.append((d, t)))
    assert calls == []


@pytest.mark.parametrize("n", [1, 4])
def test_disk_job_stats_exclude_the_consumers_own_time(tmp_path, n):
    """`disk <route>` is how long the job waited on the disk — not how long
    the consumer took between results (settlement, progress, a tar write)."""
    paths = [tmp_path / f"f{i}" for i in range(10)]
    for p in paths:
        p.write_bytes(b"x")
    with DiskJob("t", limits=Limits(read_threads=n)) as job:
        for _ in job.stat(paths):
            time.sleep(0.05)
        assert job.stats.timings()["disk read-meta"] < 0.25
