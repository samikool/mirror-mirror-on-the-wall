"""Guards on the frontend/backend boundary: long backend passes must not
starve the event loop or hold the pool lock for their whole duration.

Both of these were real: the classify loop's yield sat inside the "already
pooled" branch, so a *fresh* sync (nothing pooled yet) never yielded at all and
the sync TUI froze at startup; batch_contains held the single shared pool
connection across all ~100 of its chunked queries, blocking every other pool
caller for the length of a verify.
"""
import asyncio
import concurrent.futures
import threading
import time
from pathlib import Path

import pytest

from mmw.core.downloader import download_mirror
from mmw.core.pool import Pool
from mmw.providers.base import PackageInfo


def _packages(n: int) -> list[PackageInfo]:
    return [
        PackageInfo(
            name=f"p{i}", url=f"http://x/p{i}.deb", checksum=f"{i:064x}",
            checksum_type="sha256", size=100, repo_path=f"pool/p{i}.deb",
            filename=f"p{i}.deb",
        )
        for i in range(n)
    ]


@pytest.mark.parametrize("seed_pool", [False, True], ids=["fresh-sync", "all-pooled"])
def test_classify_never_stalls_the_event_loop(tmp_path, seed_pool):
    """No single stretch of classify may block the loop long enough to be seen.

    Tick *count* is the wrong measure — the pass is quick, so few 60 Hz ticks
    fit into it either way. What a user notices is the worst gap between UI
    frames, so that is what this asserts.
    """
    n = 200_000
    pool = Pool(tmp_path)
    pkgs = _packages(n)
    if seed_pool:
        for p in pkgs[:1000]:
            pool.add(b"x", p.checksum, "sha256", p.filename, 1)

    async def scenario():
        gaps: list[float] = []
        stop = asyncio.Event()

        async def ticker():
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(1 / 60)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        ex = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        t = asyncio.create_task(ticker())
        try:
            await download_mirror(
                packages=pkgs, metadata=[], pool=pool, mirror_name="m",
                max_connections=2, executor=ex, dry_run=True,
            )
        finally:
            stop.set()
            await t
            ex.shutdown(wait=False)
        return max(gaps) if gaps else 0.0

    # Best of three: a loaded CI box can add scheduling noise to any one run,
    # but a real regression is slow in every run. Measured on this code the
    # worst gap is 58-70 ms; with the yield in the wrong branch it was 150-246 ms.
    # The residual is GIL contention from batch_contains in the executor, not
    # the classify loop itself.
    worst = min(asyncio.run(scenario()) for _ in range(3))
    pool.close()
    assert worst < 0.12, f"event loop stalled {worst * 1000:.0f} ms during classify"


def test_batch_contains_does_not_hold_the_lock_across_chunks(tmp_path):
    """Another pool caller must make progress while a big batch_contains runs."""
    pool = Pool(tmp_path)
    n = 60_000
    with pool._lock:
        pool._conn.executemany(
            "INSERT OR IGNORE INTO packages "
            "(checksum, checksum_type, pool_path, filename, size) VALUES (?,?,?,?,?)",
            [(f"{i:064x}", "sha256", f"/x/{i}", f"p{i}.deb", 100) for i in range(n)],
        )
        pool._conn.commit()

    checksums = [f"{i:064x}" for i in range(n)]
    other_calls = 0
    running = threading.Event()
    running.set()

    def hammer():
        nonlocal other_calls
        while running.is_set():
            pool.contains("deadbeef")
            other_calls += 1
            time.sleep(0.001)

    th = threading.Thread(target=hammer, daemon=True)
    th.start()
    time.sleep(0.02)
    before = other_calls
    pool.batch_contains(checksums)
    during = other_calls - before
    running.clear()
    th.join(timeout=2)
    pool.close()

    # With the lock held across every chunk this was ~0; per-chunk locking lets
    # the other caller interleave freely.
    assert during >= 3, f"pool lock starved a concurrent caller ({during} calls got through)"


# ── the sync footer must never scan the pool on the event loop ───────────────

def _calls(func) -> set[str]:
    """Method names called in a function body. AST, not text: a docstring that
    *mentions* `pool.stats()` is documentation, not a call."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _attrs(func) -> set[str]:
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    return {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}


def test_the_footer_renders_without_touching_the_pool():
    """`refresh_stats` takes a snapshot. It used to take the `Pool` itself and
    run a full `COUNT(*) + SUM(size)` scan from a 60 Hz timer callback."""
    import inspect

    from mmw.tui.sync import StatsFooter

    params = list(inspect.signature(StatsFooter.refresh_stats).parameters)
    assert "pool" not in params, "the footer must not be handed a pool"
    assert "stats" not in _calls(StatsFooter.refresh_stats), \
        "the footer must not query the pool"


class _GatedPool:
    """A pool whose `stats()` blocks until the test opens `gate` — so "a scan
    is in flight" is a fact the test controls, never a race with how fast a
    loaded machine starts a thread. (Timed sleeps here failed a third of the
    time under parallel load: the scan's thread had not even started.)"""

    def __init__(self):
        self.gate = threading.Event()
        self.started = threading.Event()
        self.calls = 0
        self.threads: set[str] = set()

    def stats(self) -> dict:
        self.calls += 1
        self.threads.add(threading.current_thread().name)
        self.started.set()
        self.gate.wait(30)
        return {"packages": 1, "total_size": 2}


async def _until(cond, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < deadline, "condition never became true"
        await asyncio.sleep(0.01)


async def _gate_the_pool(view, gated) -> None:
    """Swap `gated` in once the view's own pool is open and idle: the view
    opens its real pool on a thread at mount (which would overwrite an earlier
    swap), and a scan it just finished would make the 5 s spacing skip ours."""
    await _until(lambda: view._pool is not None and not view._pool_stats_inflight)
    view._pool = gated
    view._pool_stats_at = 0.0


def _sync_host(tmp_path):
    from mmw.core import config
    from mmw.tui.app import MmwApp
    cfg = config.Config(
        settings=config.Settings(mirror_dir=tmp_path, max_connections=2), sources=[])
    # The machinery lives on SyncView; its thread worker needs a running app,
    # so the Sync section hosts it.
    return MmwApp(cfg, tmp_path, initial_section="sync", start_sync_with=([], False))


def test_the_pool_scan_runs_off_the_event_loop(tmp_path):
    """The freeze: a 9-14 s scan ran inside the tick callback, so the UI never
    repainted and the 32 download workers blocked behind the pool lock.

    With the gate shut, a scan run on the loop would block the trigger until
    the safety timer opens it (3 s) — so "returned at once" is the check."""
    gated = _GatedPool()

    async def scenario():
        host = _sync_host(tmp_path)
        async with host.run_test():
            view = host.active_sync
            await _gate_the_pool(view, gated)
            loop_name = threading.current_thread().name
            safety = threading.Timer(3.0, gated.gate.set)
            safety.start()
            try:
                t = time.monotonic()
                view._maybe_refresh_pool_stats(time.monotonic())
                returned_in = time.monotonic() - t
                await _until(gated.started.is_set)
            finally:
                gated.gate.set()
                safety.cancel()
            assert returned_in < 1.0, f"the trigger blocked for {returned_in:.2f}s"
            assert gated.threads and loop_name not in gated.threads, \
                f"stats() ran on the event loop thread ({gated.threads})"

    asyncio.run(scenario())


def test_only_one_pool_scan_is_ever_in_flight(tmp_path):
    """Ticks arrive at 60 Hz. Without the guard they queue scans faster than
    the scans complete."""
    gated = _GatedPool()

    async def scenario():
        host = _sync_host(tmp_path)
        async with host.run_test():
            view = host.active_sync
            await _gate_the_pool(view, gated)
            try:
                view._maybe_refresh_pool_stats(time.monotonic())
                await _until(gated.started.is_set)          # one scan, provably in flight
                for _ in range(50):
                    view._maybe_refresh_pool_stats(time.monotonic())
                    await asyncio.sleep(0.005)
                assert gated.calls == 1, f"{gated.calls} concurrent scans queued up"
            finally:
                gated.gate.set()
            await _until(lambda: not view._pool_stats_inflight)
            assert gated.calls == 1

    asyncio.run(scenario())


def test_the_scan_interval_is_measured_from_completion(tmp_path):
    """The exact bug: the clock was stamped *before* the call, so after a 14 s
    scan the 1 Hz gate was already 13 s stale and the next tick started another
    scan immediately — back-to-back scans with no gap at all."""
    from mmw.core import config
    from mmw.tui.sync import SyncView

    cfg = config.Config(
        settings=config.Settings(mirror_dir=tmp_path, max_connections=2), sources=[])
    app = SyncView(cfg, mirrors=[])

    started = time.monotonic()
    app._pool_stats_inflight = True
    time.sleep(0.05)                       # stand in for a long scan
    app._on_pool_stats({"packages": 1, "total_size": 2})

    assert app._pool_stats_at >= started, "timestamp predates the scan finishing"
    assert app._pool_stats_inflight is False
    # ...and the gate is genuinely closed for the full interval afterwards.
    app._maybe_refresh_pool_stats(app._pool_stats_at + 0.1)
    assert app._pool_stats_inflight is False, "gate reopened immediately"


def test_a_failed_scan_releases_the_guard(tmp_path):
    """A wedged guard would silently stop the footer updating for the whole run."""
    from mmw.core import config
    from mmw.tui.sync import SyncView

    cfg = config.Config(
        settings=config.Settings(mirror_dir=tmp_path, max_connections=2), sources=[])
    app = SyncView(cfg, mirrors=[])
    app._pool_stats_inflight = True
    app._on_pool_stats(None)

    assert app._pool_stats_inflight is False
    assert app._pool_stats == {"packages": 0, "total_size": 0}, "kept the last good value"


def test_expensive_pool_aggregates_never_take_the_write_lock(tmp_path):
    """`stats()` and `mirror_stats()` must not queue behind 32 download workers
    holding `self._lock` — that made the scan and the workers block each other."""
    for name in ("stats", "mirror_stats", "group_unique_count", "unique_stats"):
        assert "_lock" not in _attrs(getattr(Pool, name)), \
            f"Pool.{name} still takes the write lock"


def test_read_only_aggregates_still_see_committed_writes(tmp_path):
    """A short-lived read-only connection is only safe if it sees what `add()`
    committed a moment earlier."""
    pool = Pool(tmp_path)
    try:
        src = tmp_path / "x.deb"
        src.write_bytes(b"hello")
        pool.add(src, f"{1:064x}", "sha256", "x.deb", 5)
        assert pool.stats() == {"packages": 1, "total_size": 5}
    finally:
        pool.close()


# ── readers must not queue behind a long write ───────────────────────────────

def test_a_long_write_does_not_block_readers(tmp_path):
    """The stall Sam saw once the sync TUI was smooth enough to notice it.

    `reconcile_refs` is deliberately one transaction (readers see the old ref
    set or the new one, never a mix) and was measured at **2.3 s** for a 65k-ref
    mirror. While it held the single shared lock, every *other* mirror's
    `batch_contains` stopped dead — so a dozen repos sat waiting on the DB while
    one of them committed.
    """
    pool = Pool(tmp_path)
    try:
        # Seed enough refs that the reconcile transaction takes real time.
        entries = [(f"{i:064x}", f"pool/p{i}.deb", "pkg") for i in range(60_000)]
        pool.reconcile_refs("seed", entries, "https://x/seed")

        reader_gaps: list[float] = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                t0 = time.monotonic()
                pool.batch_contains([f"{i:064x}" for i in range(50)])
                reader_gaps.append(time.monotonic() - t0)

        th = threading.Thread(target=reader, daemon=True)
        th.start()
        time.sleep(0.05)
        t0 = time.monotonic()
        pool.reconcile_refs("other", entries, "https://x/other")
        write_secs = time.monotonic() - t0
        stop.set()
        th.join(timeout=5)

        assert reader_gaps, "the reader never ran"
        worst = max(reader_gaps)
        # The reader must never be stalled for anything close to the write.
        assert worst < max(0.25, write_secs / 4), (
            f"a reader waited {worst:.2f}s behind a {write_secs:.2f}s write"
        )
    finally:
        pool.close()


def test_a_reader_sees_a_write_that_just_committed(tmp_path):
    """A separate read connection is only safe if it observes committed writes
    immediately — WAL guarantees this, but it is the whole correctness basis."""
    pool = Pool(tmp_path)
    try:
        src = tmp_path / "x.deb"
        src.write_bytes(b"hello")
        assert pool.contains(f"{7:064x}") is False
        pool.add(src, f"{7:064x}", "sha256", "x.deb", 5)
        assert pool.contains(f"{7:064x}") is True, "reader missed a committed write"

        pool.reconcile_refs("m", [(f"{7:064x}", "pool/x.deb", "pkg")], "https://x/y")
        assert pool.ref_map("m") == {"pool/x.deb": f"{7:064x}"}
        assert [n for n, *_ in pool.list_mirrors()] or True   # smoke: no exception
    finally:
        pool.close()


def test_reads_still_work_without_a_separate_reader(tmp_path):
    """An in-memory pool (nothing synced yet, `read_only=True` with no db file)
    has no file to open a second connection on, so it falls back to the shared
    connection rather than failing.

    A `read_only=True` pool that DOES have a db file is the opposite case —
    see `test_read_only_pool_gets_a_dedicated_reader_too` below: it gets the
    same dedicated, tuned reader a writable pool does, precisely because it
    has no writer of its own to justify skipping that (see pool.py's `_read()`
    for the incident this documents: verify/bundle's concurrent per-mirror
    reads used to serialize on one untuned connection instead)."""
    empty = Pool(tmp_path / "nothing", read_only=True)      # in-memory fallback
    try:
        assert empty._rconn is None
        assert empty.contains("deadbeef") is False
        assert empty.batch_contains(["deadbeef"]) == set()
        assert empty.list_mirrors() == []
    finally:
        empty.close()


def test_read_only_pool_gets_a_dedicated_reader_too(tmp_path):
    w = Pool(tmp_path)
    try:
        src = tmp_path / "y.deb"
        src.write_bytes(b"hi")
        w.add(src, f"{9:064x}", "sha256", "y.deb", 2)
    finally:
        w.close()

    ro = Pool(tmp_path, read_only=True)
    try:
        assert ro._rconn is not None, (
            "a read-only pool with a db file gets the same tuned dedicated "
            "reader a writable pool does — see pool.py's _read()"
        )
        assert ro.contains(f"{9:064x}") is True
    finally:
        ro.close()


def test_every_pool_write_takes_the_write_lock_and_every_read_does_not(tmp_path):
    """Structural guard: a new read method that grabs `self._lock` silently
    reintroduces the stall; a write that uses the read connection would fail at
    runtime, since that connection is opened `mode=ro`."""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(Pool)))
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
        if fn.name in ("__init__", "_read"):
            continue
        dumped = ast.dump(fn)
        # The docstring is prose, not SQL. Joining every string constant in the
        # function meant a method whose docstring merely *explained* that
        # committing an empty set would "delete the mirror's whole index" was
        # classified as a writer and failed this assertion — a false positive
        # that punishes explaining yourself. Only real statements are scanned.
        body = fn.body[1:] if ast.get_docstring(fn) is not None else fn.body
        sql = " ".join(
            s.value
            for stmt in body
            for s in ast.walk(stmt)
            if isinstance(s, ast.Constant) and isinstance(s.value, str)
        ).upper()
        if not any(k in sql for k in ("SELECT", "INSERT", "DELETE", "UPDATE")):
            continue
        mutates = any(k in sql for k in ("INSERT", "DELETE", "UPDATE"))
        uses_write_lock = "'_lock'" in dumped and "_read" not in dumped
        if mutates:
            assert uses_write_lock, f"Pool.{fn.name} writes without the write lock"
        else:
            assert not uses_write_lock, f"Pool.{fn.name} reads under the write lock"


def test_the_reader_is_tuned_for_a_disk_bound_workload(tmp_path):
    """This workload is disk-bound, not lock-bound. Adding read *connections*
    measured worse (1 conn 350 q/s, 4 conns 190, 8 conns 107 — each connection
    keeps its own page cache, so hot pages are re-read per connection); the
    lever that actually works is a bigger cache and mmap, measured at ~1.4x."""
    pool = Pool(tmp_path)
    try:
        src = tmp_path / "x.deb"
        src.write_bytes(b"hi")
        pool.add(src, f"{1:064x}", "sha256", "x.deb", 2)
    finally:
        pool.close()

    pool = Pool(tmp_path)
    try:
        assert pool._rconn is not None
        cache = pool._rconn.execute("PRAGMA cache_size").fetchone()[0]
        assert cache <= -65536, f"reader cache not raised (got {cache})"
        assert pool._rconn.execute("PRAGMA mmap_size").fetchone()[0] > 0
    finally:
        pool.close()


# ── the footer's "Transferred" figure ────────────────────────────────────────

def _card():
    from mmw.core import config
    from mmw.tui.sync import MirrorCard

    return MirrorCard(config.MirrorConfig(
        name="m", provider="apt", source="https://x/y", source_url="https://x/y",
        arch="arm64", suite="noble", component="main"))


def test_transferred_bytes_move_while_a_package_is_still_downloading():
    """The bug Sam saw: `Transferred` summed *completed packages*, so a mirror
    pulling multi-gigabyte CUDA packages showed a frozen number for minutes
    while the speed display kept moving."""
    card = _card()
    assert card.bytes_transferred == 0

    for _ in range(16):
        card.add_chunk_bytes(65536)          # one package, still in flight

    assert card.bytes_transferred == 16 * 65536, "mid-download bytes are invisible"


def test_transferred_bytes_are_not_double_counted_on_completion():
    """Chunks and the completed-package total must not both be added."""
    from mmw.core.downloader import Status

    card = _card()
    for _ in range(16):
        card.add_chunk_bytes(65536)
    card.accumulate("pkg.deb", Status.DOWNLOADED, 16 * 65536)

    assert card.bytes_transferred == 16 * 65536


def test_speed_accounting_still_drains_independently():
    """The EMA drains `_chunk_bytes`; the cumulative total must survive it."""
    card = _card()
    card.add_chunk_bytes(1_000_000)
    card._speed_last_time = time.monotonic() - 1.0
    card._update_speed(time.monotonic())

    assert card._chunk_bytes == 0, "speed did not drain its counter"
    assert card.bytes_transferred == 1_000_000, "draining speed lost the total"


def test_a_finishing_card_lands_its_last_bytes():
    """`_tick` stops flushing a card once it is done, so anything still pending
    at that moment would never reach the counters."""
    from mmw.core.downloader import Status

    card = _card()
    card._start = time.monotonic()
    card.accumulate("pkg.deb", Status.POOLED, 0)
    assert card._pending_pooled == 1

    card.finish()
    assert card._done is True
    assert card._pending_pooled == 0, "pending counters were dropped on finish"
    assert card._pooled == 1


# ── the pool write must not block the download thread ────────────────────────

def test_a_queued_row_is_visible_to_reads_immediately(tmp_path):
    """A blob is hardlinked into place before its row is queued, so it *is*
    present. If `contains` missed it, classify would re-download what we
    already have."""
    pool = Pool(tmp_path)
    try:
        cs = [f"{i:064x}" for i in range(5)]
        for i, c in enumerate(cs):
            src = tmp_path / f"x{i}"
            src.write_bytes(b"hi")
            pool.add(src, c, "sha256", f"x{i}.deb", 2)
        assert all(pool.contains(c) for c in cs), "queued row invisible to contains()"
        assert pool.batch_contains(cs) == set(cs)
    finally:
        pool.close()


def test_reconcile_commits_queued_rows_first(tmp_path):
    """Refs are joined against `packages` at publish time, so a ref must never
    point at a row still sitting in the queue."""
    pool = Pool(tmp_path)
    try:
        src = tmp_path / "x"
        src.write_bytes(b"hi")
        pool.add(src, f"{1:064x}", "sha256", "x.deb", 2)
        pool.reconcile_refs("m", [(f"{1:064x}", "pool/x.deb", "pkg")], "https://x/y")
        assert pool.stats()["packages"] == 1
        assert pool.mirror_files(["m"]), "reconcile ran before the row was committed"
    finally:
        pool.close()


def test_closing_commits_what_is_queued(tmp_path):
    pool = Pool(tmp_path)
    src = tmp_path / "x"
    src.write_bytes(b"hi")
    pool.add(src, f"{2:064x}", "sha256", "x.deb", 2)
    pool.close()                       # must not drop the row

    reopened = Pool(tmp_path, read_only=True)
    try:
        assert reopened.contains(f"{2:064x}"), "close() lost a queued row"
    finally:
        reopened.close()


def test_the_footer_count_never_lags_behind_disk(tmp_path):
    """`stats()` is what the sync footer shows. A count that silently trails the
    packages already on disk reads as a stalled sync."""
    pool = Pool(tmp_path)
    try:
        for i in range(3):
            src = tmp_path / f"y{i}"
            src.write_bytes(b"xyz")
            pool.add(src, f"{i + 10:064x}", "sha256", f"y{i}.deb", 3)
        assert pool.stats() == {"packages": 3, "total_size": 9}
    finally:
        pool.close()


def test_deferred_writes_can_be_turned_off(tmp_path, monkeypatch):
    """`MMW_DEFER_WRITES=0` restores the inline path without a code change —
    the way back if the new path ever behaves worse."""
    monkeypatch.setenv("MMW_DEFER_WRITES", "0")
    pool = Pool(tmp_path)
    try:
        assert pool._writer is None
        src = tmp_path / "x"
        src.write_bytes(b"hi")
        pool.add(src, f"{3:064x}", "sha256", "x.deb", 2)
        assert pool.contains(f"{3:064x}")
    finally:
        pool.close()


def test_a_read_only_pool_never_starts_a_writer(tmp_path):
    pool = Pool(tmp_path / "nothing", read_only=True)
    try:
        assert pool._writer is None
    finally:
        pool.close()


def test_a_failing_write_is_reported_not_swallowed(tmp_path):
    """A sync must not silently lose package rows."""
    pool = Pool(tmp_path)
    try:
        src = tmp_path / "x"
        src.write_bytes(b"hi")
        pool.add(src, f"{4:064x}", "sha256", "x.deb", 2)
        pool.flush_writes()
        pool._conn.execute("DROP TABLE packages")
        pool._conn.commit()
        src2 = tmp_path / "y"
        src2.write_bytes(b"hi")
        pool.add(src2, f"{5:064x}", "sha256", "y.deb", 2)
        assert pool.flush_writes(), "a failed batch reported nothing"
    finally:
        pool.close()


def test_reconcile_runs_on_the_writer_not_a_download_thread(tmp_path):
    """Profiled after the `add()` fix: `reconcile_refs` was 46% of a worker's
    samples at 1.1% CPU — a download slot held open, blocked on the write lock
    for a ~2.3 s transaction. It belongs on the thread that already owns that
    lock's rhythm."""
    pool = Pool(tmp_path)
    ran_on: list[str] = []
    try:
        real = pool.reconcile_refs

        def spy(*a, **kw):
            ran_on.append(threading.current_thread().name)
            return real(*a, **kw)

        pool.reconcile_refs = spy          # type: ignore[method-assign]
        done = threading.Event()
        assert pool.submit_reconcile("m", [], "https://x/y", lambda err: done.set())
        assert done.wait(10), "reconcile never completed"
        assert ran_on == ["mmw-pool-writer"], ran_on
    finally:
        pool.close()


def test_a_queued_reconcile_sees_rows_queued_before_it(tmp_path):
    """Ordering is the correctness requirement: refs are joined against
    `packages`, so a ref must never be committed ahead of its row."""
    pool = Pool(tmp_path)
    try:
        src = tmp_path / "x"
        src.write_bytes(b"hi")
        pool.add(src, f"{1:064x}", "sha256", "x.deb", 2)
        done = threading.Event()
        pool.submit_reconcile(
            "m", [(f"{1:064x}", "pool/x.deb", "pkg")], "https://x/y", lambda err: done.set())
        assert done.wait(10)
        assert len(pool.mirror_files(["m"])) == 1, "ref committed without its package row"
    finally:
        pool.close()


def test_a_failing_reconcile_reports_back(tmp_path):
    """The sync's atomic commit must never fail silently."""
    pool = Pool(tmp_path)
    seen: list = []
    try:
        pool.reconcile_refs = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]
        done = threading.Event()
        pool.submit_reconcile("m", [], "https://x/y",
                              lambda err: (seen.append(err), done.set()))
        assert done.wait(10)
        assert isinstance(seen[0], RuntimeError), seen
    finally:
        pool.close()


def test_reconcile_falls_back_when_deferred_writes_are_off(tmp_path, monkeypatch):
    monkeypatch.setenv("MMW_DEFER_WRITES", "0")
    pool = Pool(tmp_path)
    try:
        assert pool.submit_reconcile("m", [], "https://x/y", lambda err: None) is False
    finally:
        pool.close()


def test_flushing_from_the_writer_thread_does_not_deadlock(tmp_path):
    """`reconcile_refs` flushes first. Run on the writer, that would otherwise
    wait for the very thread executing it."""
    pool = Pool(tmp_path)
    try:
        src = tmp_path / "x"
        src.write_bytes(b"hi")
        pool.add(src, f"{6:064x}", "sha256", "x.deb", 2)
        done = threading.Event()
        pool.submit_reconcile("m", [(f"{6:064x}", "p", "pkg")], "https://x/y",
                              lambda err: done.set())
        assert done.wait(10), "deadlocked flushing from the writer thread"
    finally:
        pool.close()


def test_metadata_membership_is_checked_in_one_batch(tmp_path):
    """Profiled after the reconcile fix: `pool.contains <- _fetch_or_skip` was
    the top stack on every worker, 40% of samples blocked in `pool._read`. The
    check runs once per metadata file across all 32 threads at once, so it must
    be batched, not parallelised."""
    import ast
    import inspect
    import textwrap

    from mmw.core.downloader import _fetch_or_skip

    params = list(inspect.signature(_fetch_or_skip).parameters)
    assert "pool" not in params, "_fetch_or_skip must not hold a Pool"

    tree = ast.parse(textwrap.dedent(inspect.getsource(_fetch_or_skip)))
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "contains" not in called, "still doing a locked lookup per file"


def test_a_batched_membership_set_still_skips_pooled_metadata(tmp_path):
    """The batching must not change the outcome — a pooled blob still skips
    the network."""
    import hashlib

    from mmw.core.downloader import Fetch, _fetch_or_skip
    from mmw.providers.base import MetadataFile

    data = b"index-bytes"
    sha = hashlib.sha256(data).hexdigest()
    pool = Pool(tmp_path)
    try:
        pool.add(data, sha, "sha256", "Packages.gz", len(data))
        pooled = pool.batch_contains([sha])
        assert sha in pooled, "queued row missing from the batched set"
        mf = MetadataFile(url="http://x/f", repo_path="p/Packages.gz", expected_sha256=sha)
        assert _fetch_or_skip(mf, pooled)[0] is Fetch.ALREADY_DONE
    finally:
        pool.close()


# ── downloads run on a fixed consumer pool, not a task per package ───────────

def _pkgs(n: int):
    from mmw.providers.base import PackageInfo

    return [
        PackageInfo(name=f"p{i}", url=f"http://x/p{i}.deb", checksum=f"{i:064x}",
                    checksum_type="sha256", size=10, repo_path=f"pool/p{i}.deb",
                    filename=f"p{i}.deb")
        for i in range(n)
    ]


def test_downloads_do_not_allocate_a_task_per_package(tmp_path, monkeypatch):
    """`asyncio.gather(*[_do_one(p) for p in to_download])` created one Task per
    package — 65,000 for one ubuntu-ports component — and queued every one on
    the shared semaphore. Profiled at **52.9% of the event-loop thread** inside
    `Semaphore._wake_up_next`, plus ~1.5 GB RSS in Task objects.

    Task count is sampled from the event loop, never from the worker thread:
    `asyncio.all_tasks()` raises without a running loop, and an exception there
    reads as a failed download and triggers the retry path.
    """
    import mmw.core.downloader as dl

    n_pkgs = 400
    calls = [0]

    def fake_download(pkg, pool, chunk_cb, start_cb, stop_ev, resume=False):
        calls[0] += 1
        return dl.DownloadResult(dl.Status.DOWNLOADED, Path(pkg.filename),
                                 bytes_transferred=10, checksum=pkg.checksum)

    monkeypatch.setattr(dl, "_download_sync", fake_download)

    async def scenario():
        peak = 0
        done = asyncio.Event()

        async def watch():
            nonlocal peak
            while not done.is_set():
                peak = max(peak, len(asyncio.all_tasks()))
                await asyncio.sleep(0.001)

        pool = Pool(tmp_path)
        watcher = asyncio.create_task(watch())
        try:
            summary = await dl.download_mirror(
                mirror_name="m", packages=_pkgs(n_pkgs), metadata=[], pool=pool,
                max_connections=8, source="https://x/y",
            )
        finally:
            done.set()
            await watcher
            pool.close()
        return summary, peak

    summary, peak = asyncio.run(scenario())
    assert calls[0] == n_pkgs, f"{calls[0]} calls for {n_pkgs} packages (retries?)"
    assert summary.downloaded == n_pkgs
    # 8 consumers + gather parent + watcher + main, nowhere near one per package.
    assert peak < 40, f"{peak} concurrent tasks for {n_pkgs} packages"


def test_the_consumer_pool_is_capped_by_max_connections(tmp_path, monkeypatch):
    """Concurrency must still be bounded — the semaphore bounds it globally,
    and the pool size must not exceed what was asked for."""
    import mmw.core.downloader as dl

    live = [0]
    peak = [0]

    def fake_download(pkg, pool, chunk_cb, start_cb, stop_ev, resume=False):
        live[0] += 1
        peak[0] = max(peak[0], live[0])
        time.sleep(0.002)
        live[0] -= 1
        return dl.DownloadResult(dl.Status.DOWNLOADED, Path(pkg.filename),
                                 bytes_transferred=10, checksum=pkg.checksum)

    monkeypatch.setattr(dl, "_download_sync", fake_download)

    async def scenario():
        pool = Pool(tmp_path)
        try:
            return await dl.download_mirror(
                mirror_name="m", packages=_pkgs(200), metadata=[], pool=pool,
                max_connections=4, source="https://x/y",
            )
        finally:
            pool.close()

    summary = asyncio.run(scenario())
    assert summary.downloaded == 200
    assert peak[0] <= 4, f"{peak[0]} concurrent downloads with max_connections=4"


def test_fewer_packages_than_workers_still_completes(tmp_path, monkeypatch):
    import mmw.core.downloader as dl

    monkeypatch.setattr(dl, "_download_sync", lambda pkg, *a: dl.DownloadResult(
        dl.Status.DOWNLOADED, Path(pkg.filename), bytes_transferred=10,
        checksum=pkg.checksum))

    async def scenario():
        pool = Pool(tmp_path)
        try:
            return await dl.download_mirror(
                mirror_name="m", packages=_pkgs(3), metadata=[], pool=pool,
                max_connections=32, source="https://x/y",
            )
        finally:
            pool.close()

    assert asyncio.run(scenario()).downloaded == 3


def test_a_stop_event_drains_the_consumers(tmp_path, monkeypatch):
    """Stopping must not leave the remaining queue running."""
    import mmw.core.downloader as dl

    stop = threading.Event()
    seen = [0]

    def fake_download(pkg, pool, chunk_cb, start_cb, stop_ev, resume=False):
        seen[0] += 1
        if seen[0] == 10:
            stop.set()
        return dl.DownloadResult(dl.Status.DOWNLOADED, Path(pkg.filename),
                                 bytes_transferred=10, checksum=pkg.checksum)

    monkeypatch.setattr(dl, "_download_sync", fake_download)

    async def scenario():
        pool = Pool(tmp_path)
        try:
            return await dl.download_mirror(
                mirror_name="m", packages=_pkgs(500), metadata=[], pool=pool,
                max_connections=4, source="https://x/y", stop_event=stop,
            )
        finally:
            pool.close()

    summary = asyncio.run(scenario())
    assert summary.stopped is True
    assert seen[0] < 500, "kept downloading after the stop event"


def test_the_sync_lock_is_never_taken_on_the_event_loop(tmp_path, monkeypatch):
    """A sync must stay responsive while something else holds the pool.

    `download_mirror` is a coroutine — in the TUI it runs on the event loop as
    an async Textual worker — and the shared pool lock does `mkdir` + `open` +
    `flock`. Taking those inline froze a real 45-mirror sync solid: a profile
    of that run put the MainThread at 21.8% of samples inside
    `shared_pool_lock`, under `download_mirror <- sync_mirror <- _sync_mirror`,
    with `flock(LOCK_SH)` waiting on the kernel and the UI painting nothing.

    Here the exclusive lock is genuinely held for a moment, which is the worst
    case: acquisition really does have to wait. The loop must keep ticking
    throughout, because the wait belongs on a worker thread.
    """
    import fcntl

    import mmw.core.downloader as dl
    from mmw.core.pool import _lock_path, release_sync_lock

    monkeypatch.setattr(
        dl, "_download_sync",
        lambda pkg, pool, chunk_cb, start_cb, stop_ev, resume=False: dl.DownloadResult(
            dl.Status.DOWNLOADED, Path(pkg.filename), bytes_transferred=10,
            checksum=pkg.checksum,
        ),
    )

    pool = Pool(tmp_path)
    lock_path = _lock_path(tmp_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    blocker = open(lock_path, "a+")
    fcntl.flock(blocker.fileno(), fcntl.LOCK_EX)

    ticks = [0]

    async def heartbeat():
        # What the UI's 60 Hz repaint is, stripped to its essentials.
        while True:
            ticks[0] += 1
            await asyncio.sleep(0.005)

    # Released from a TIMER THREAD, not from the coroutine. If the lock is ever
    # taken on the loop again, the loop is frozen and could not run its own
    # release — the test would hang the whole suite instead of failing. This
    # way the bug reports itself as a tick count of ~0.
    released = threading.Timer(0.3, lambda: fcntl.flock(blocker.fileno(), fcntl.LOCK_UN))

    async def scenario():
        beat = asyncio.ensure_future(heartbeat())
        released.start()
        sync = asyncio.ensure_future(dl.download_mirror(
            mirror_name="m", packages=_pkgs(3), metadata=[], pool=pool,
            max_connections=4, source="https://x/y",
        ))
        await asyncio.sleep(0.3)          # the lock is held for all of this
        held = ticks[0]
        await sync
        beat.cancel()
        return held

    try:
        held = asyncio.run(scenario())
    finally:
        released.cancel()
        blocker.close()
        release_sync_lock()
        pool.close()

    # ~60 ticks are due in 0.3s; anything in double figures proves the loop was
    # never parked in the kernel. Before the fix this was 0 and stayed 0.
    assert held > 10, f"event loop only ticked {held}x while the pool was locked"


def test_sync_opens_its_pool_off_the_mount_path(tmp_path):
    """`on_mount` runs on the event loop, so opening the pool there stalled the
    first frame: a mkdir, a SQLite connect, the schema script and a sweep of
    `.pool/tmp/`. It is deferred to a thread now, which means the app must
    still end up with a usable pool — and must not fall over when it has none
    yet.

    Mounted with `mirrors=[]` on purpose: a sync starts real HTTP for every
    mirror it is given, which is why it is absent from the layout suites.
    """
    from mmw.core import config
    from mmw.tui.app import MmwApp

    cfg = config.Config(
        settings=config.Settings(mirror_dir=tmp_path / "m", max_connections=2),
        sources=[])

    async def scenario():
        app = MmwApp(cfg, tmp_path, initial_section="sync", start_sync_with=([], False))
        async with app.run_test() as pilot:
            await pilot.pause()
            # The tick runs at 60 Hz from the moment of mount and must survive
            # `self._pool` still being None.
            deadline = time.monotonic() + 5.0
            while app.active_sync._pool is None and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            return app.active_sync._pool is not None

    assert asyncio.run(scenario()), "the pool was never opened"
    assert (tmp_path / "m" / ".pool").is_dir(), "pool directory was not created"


# Calls that hit the disk (or the kernel) and must never run on the event loop.
_BLOCKING_CALLS = {
    "mkdir", "iterdir", "rglob", "glob", "read_bytes", "write_bytes",
    "read_text", "write_text", "unlink", "rmtree", "disk_usage", "flock",
    "walk", "copy2",
}
_BLOCKING_NAMES = {"Pool", "open", "input"}
_BLOCKING_ATTRS = {
    "save", "load", "delete", "wipe", "prune_orphans", "read_key", "find_keys",
    "fetch_key", "probe", "probe_all", "probe_suite", "stats", "mirror_stats", "unique_stats",
    "network_totals", "dst_packages",
    "latest_manifest_path", "acquire_sync_lock", "exclusive_pool_lock",
    "shared_pool_lock", "pool_is_busy", "reconcile_refs", "flush_writes",
}
# Handed to a worker — the whole point.
_OFFLOADERS = {"to_thread", "run_in_executor", "call_from_thread"}


def _runs_on_the_loop(fn) -> bool:
    """True for a function Textual invokes on the event loop.

    `@work(thread=True)` is the escape hatch; everything else — an `async def`,
    an `@on(...)` handler, `on_mount`, an action, a watcher, the tick — runs on
    the loop.
    """
    import ast

    for d in fn.decorator_list:
        if (isinstance(d, ast.Call) and getattr(d.func, "id", "") == "work"
                and any(k.arg == "thread" and getattr(k.value, "value", False) is True
                        for k in d.keywords)):
            return False
    if isinstance(fn, ast.AsyncFunctionDef):
        return True
    return (fn.name.startswith(("on_", "action_", "watch_", "compose", "_tick"))
            or any(isinstance(d, ast.Call) and getattr(d.func, "id", "") == "on"
                   for d in fn.decorator_list))


def _direct_calls(fn):
    """Calls in `fn`'s own body, not inside a nested def or lambda — those are
    what get passed to `to_thread` and run somewhere else."""
    import ast

    out = []

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Call):
                out.append(child)
            walk(child)

    walk(fn)
    return out


def test_no_tui_handler_does_blocking_io_on_the_event_loop():
    """The rule this project keeps relearning, as a guard rather than a habit.

    It has now been broken three separate ways: a pool `stats()` scan inside
    the 60 Hz tick (9-14 s of frozen UI), `flock` taken per mirror inside
    `download_mirror` (a sync that locked up outright), and `Pool()` itself
    built in the sync's `on_mount`. Each was found by a human noticing a freeze;
    this finds the next one at import time.

    If something here is genuinely cheap enough to stay on the loop, add it to
    `_ALLOWED` with the reason — do not widen the call sets.
    """
    import ast
    import pathlib

    _ALLOWED: set[tuple[str, str, str]] = set()

    offenders = []
    # `core/downloader.py`'s `download_mirror` and `core/sync_engine.py`'s
    # `sync_mirror` are coroutines the TUI awaits directly on the event loop —
    # which is where the worst instance of this actually happened (a per-mirror
    # `flock` that locked a 45-mirror sync solid). A guard that only watched
    # `tui/` would have missed it entirely.
    # rglob: the rebuilt TUI lives in tui/ui/ and tui/sections/ — a flat glob
    # would exempt every handler in it.
    scanned = sorted(pathlib.Path("src/mmw/tui").rglob("*.py")) + [
        pathlib.Path("src/mmw/core/downloader.py"),
        pathlib.Path("src/mmw/core/sync_engine.py"),
    ]
    for path in scanned:
        tree = ast.parse(path.read_text())
        for fn in [n for n in ast.walk(tree)
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
            if not _runs_on_the_loop(fn):
                continue
            for call in _direct_calls(fn):
                f = call.func
                name = getattr(f, "attr", None) or getattr(f, "id", None)
                if name in _OFFLOADERS or name is None:
                    continue
                if name in _BLOCKING_CALLS or name in _BLOCKING_NAMES or name in _BLOCKING_ATTRS:
                    key = (path.name, fn.name, name)
                    if key not in _ALLOWED:
                        offenders.append(f"{path.name}:{call.lineno} {fn.name}() -> {name}()")

    assert not offenders, (
        "blocking I/O on the Textual event loop:\n  " + "\n  ".join(offenders)
        + "\n\nHand it to a thread (`await asyncio.to_thread(...)`, or "
          "`@work(thread=True)`), or add it to _ALLOWED with a reason."
    )
