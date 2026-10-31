"""BatchWriter: one thread commits queued items in batches; tasks run after
everything queued before them; flush is a barrier; errors are collected."""
import threading
import time

from mmw.core.disk_io.writer import BatchWriter


def test_batch_writer_commits_by_count():
    batches = []
    w = BatchWriter(lambda items: batches.append(list(items)), name="t", batch=3, interval=60)
    for i in range(7):
        w.submit(i)
    gate = threading.Event()
    w.submit_task(lambda: None, lambda error: gate.set())   # commits what is queued first
    assert gate.wait(5)
    assert w.flush() == []
    assert batches == [[0, 1, 2], [3, 4, 5], [6]]
    w.stop()


def test_task_runs_after_prior_items_committed():
    committed, seen = [], []
    w = BatchWriter(committed.extend, name="t", batch=100, interval=60)
    for i in range(5):
        w.submit(i)
    done = threading.Event()
    w.submit_task(lambda: seen.append(list(committed)), lambda error: done.set())
    assert done.wait(5)
    assert seen == [[0, 1, 2, 3, 4]]
    w.stop()


def test_flush_commits_now_not_on_the_next_tick():
    """A flush does not sit out the batch interval: with a minute-long
    interval it still returns as soon as the pending items are committed."""
    committed = []
    w = BatchWriter(committed.extend, name="t", batch=100, interval=60)
    w.submit(1)
    w.submit(2)
    t = time.monotonic()
    assert w.flush(timeout=30) == []
    assert time.monotonic() - t < 10
    assert committed == [1, 2]
    w.stop()


def test_flush_on_writer_thread_is_noop():
    w = BatchWriter(lambda items: None, name="t", interval=0.05)
    done, result = threading.Event(), []
    w.submit_task(lambda: result.append(w.flush(timeout=5)), lambda error: done.set())
    assert done.wait(2), "flush on the writer thread waited on itself"
    assert result == [[]]
    w.stop()


def test_commit_error_is_collected_not_raised():
    def commit(items):
        raise RuntimeError("disk full")

    w = BatchWriter(commit, name="t", interval=0.05)
    w.submit("a")
    errors = w.flush()
    assert len(errors) == 1 and "disk full" in errors[0]
    w.submit("b")                                    # the writer is still alive
    assert len(w.stop()) == 2


def test_on_idle_runs_when_the_queue_drains():
    idle = threading.Event()
    w = BatchWriter(lambda items: None, name="t", interval=0.05, on_idle=idle.set)
    w.submit(1)
    w.flush()
    assert idle.wait(2)
    w.stop()


def test_stop_reports_a_writer_still_committing():
    release = threading.Event()
    w = BatchWriter(lambda items: release.wait(5), name="t", interval=0.01)
    w.submit("a")
    time.sleep(0.05)                                  # the commit is under way
    errors = w.stop(timeout=0.2)
    assert any("still committing" in e for e in errors)
    release.set()
    w.stop()


def test_a_bounded_queue_holds_back_producers():
    release, submitted = threading.Event(), []
    w = BatchWriter(lambda items: release.wait(5), name="t", batch=1, interval=0.01, maxsize=2)

    def produce():
        for i in range(10):
            w.submit(i)
            submitted.append(i)

    producer = threading.Thread(target=produce)
    producer.start()
    time.sleep(0.3)
    assert producer.is_alive() and len(submitted) < 10, "the queue never pushed back"
    release.set()
    producer.join(5)
    assert len(submitted) == 10
    w.stop()
