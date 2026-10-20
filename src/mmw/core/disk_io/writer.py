"""The db route: one thread per database commits queued rows in batches."""
import queue
import threading
import time
from collections.abc import Callable


class BatchWriter:
    """One thread commits queued items in batches (by count or interval).
    Tasks run after everything queued before them is committed. flush() is
    a barrier, and calling it on the writer thread is a no-op. Errors are
    collected, never raised into producers.

    Batching took the pool's row writes from ~1,000 adds/s to 3.4x that, and
    moving them off the producing threads freed those threads entirely (see
    `pool._DeferredWriter`). Subclasses hook `_on_tick(now)` (every loop)
    and `_on_stop()` (once, last)."""

    _SENTINEL = object()

    class _Task:
        """A unit of work that must run *after* queued items are committed."""

        __slots__ = ("fn", "done")

        def __init__(self, fn, done):
            self.fn = fn
            self.done = done

    def __init__(self, commit: Callable[[list], None], *, name: str,
                 batch: int = 500, interval: float = 0.5,
                 on_idle: Callable[[], None] | None = None, maxsize: int = 0):
        self._commit_items = commit
        self._batch = batch
        self._interval = interval
        self._on_idle = on_idle
        # Bounded (maxsize > 0): a producer outrunning the commits blocks in
        # submit() instead of piling items up in memory.
        self._q: queue.Queue = queue.Queue(maxsize)
        self._errors: list[str] = []
        self._idle = threading.Event()
        self._idle.set()
        # The writer's "queue empty → set idle" and a producer's "clear idle"
        # (after its put) are each atomic under this lock, so idle is never
        # left set with an item queued. The put itself stays outside it: a
        # full queue blocks the producer, and holding the lock there would
        # stop the writer from draining.
        self._idle_lock = threading.Lock()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    def submit(self, item) -> None:
        self._q.put(item)
        with self._idle_lock:
            self._idle.clear()

    def submit_task(self, fn, done) -> None:
        """Run `fn()` on the writer thread once queued items are committed.

        `done(error_or_None)` is called afterwards, on the writer thread.
        """
        self._q.put(self._Task(fn, done))
        with self._idle_lock:
            self._idle.clear()

    def _commit(self, items: list) -> None:
        try:
            self._commit_items(items)
        except Exception as e:                      # never lose the producers to this
            self._errors.append(f"{len(items)} item(s): {e}")

    def _on_tick(self, now: float) -> None:
        pass

    def _on_stop(self) -> None:
        pass

    def _run(self) -> None:
        pending: list = []
        last = time.monotonic()
        while True:
            try:
                item = self._q.get(timeout=self._interval)
            except queue.Empty:
                item = None
            stopping = item is self._SENTINEL
            task = item if isinstance(item, self._Task) else None
            if item is not None and not stopping and task is None:
                pending.append(item)
            if task is not None and pending:
                self._commit(pending)          # the task must see these items
                pending = []
                last = time.monotonic()
            if task is not None:
                error = None
                try:
                    task.fn()
                except Exception as e:
                    error = e
                    self._errors.append(str(e))
                try:
                    task.done(error)
                except Exception:
                    pass
            now = time.monotonic()
            if pending and (stopping or len(pending) >= self._batch
                            or now - last >= self._interval):
                self._commit(pending)
                pending = []
                last = now
            self._on_tick(now)
            if not pending:
                with self._idle_lock:
                    drained = self._q.empty()
                    if drained:
                        self._idle.set()
                if drained and self._on_idle is not None:
                    try:
                        self._on_idle()
                    except Exception as e:
                        self._errors.append(str(e))
            if stopping:
                self._on_stop()
                self._idle.set()
                return

    def flush(self, timeout: float = 60.0) -> list[str]:
        """Block until every queued item is committed. Returns any errors.

        Called *from* the writer thread this is a no-op rather than a
        deadlock: a queued task already had its items committed before it
        ran, and waiting on idle there would wait for a thread that is us.
        """
        if threading.current_thread() is self._thread:
            return list(self._errors)
        self._idle.wait(timeout)
        return list(self._errors)

    def stop(self, timeout: float | None = 60.0) -> list[str]:
        """Commit what is queued and end the thread. `timeout=None` waits
        for every item; a writer still running after `timeout` is reported
        as an error, never passed over in silence."""
        self._q.put(self._SENTINEL)
        self._thread.join(timeout)
        errors = list(self._errors)
        if self._thread.is_alive():
            errors.append(f"{self._thread.name}: still committing after {timeout}s")
        return errors
