"""A lane: `n` workers that run one kind of disk operation for one job, with
results handed back in item order as they finish."""
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from mmw.core.disk_io.stats import DiskStats


class Lane:
    """An executor of `n` workers, made on first use and reused until
    close(). n == 1 never makes threads: work runs inline on the caller's
    thread, which is exactly today's serial behaviour."""

    def __init__(self, n: int, name: str, stats: DiskStats, route: str):
        self.n = max(1, n)
        self.name = name
        self._stats = stats
        self._route = route
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()

    def _pool(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(self.n, thread_name_prefix=f"mmw-disk-{self.name}")
            return self._executor

    def close(self) -> None:
        with self._lock:
            executor, self._executor = self._executor, None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    def ordered_map(self, fn, items, *, stop_event=None, window=None):
        """Yield (item, result | Exception) for each item, in item order.

        - Each pair comes as soon as it AND every earlier item are done,
          never a run at a time. (A TUI that moved in jumps of 1,000 files
          was the bug this rule came from.)
        - At most `window` items are in flight (default 4 * n), so memory
          and the work left after a stop are bounded. `items` is pulled
          lazily — it may be a stream of millions of rows.
        - An exception that `fn` raises is yielded as that item's result,
          not raised. BaseExceptions that are not Exceptions propagate.
        - Once `stop_event` is set, nothing new is submitted. Items already
          submitted (at most `window`) finish and are yielded, in order, so
          every operation that touched the disk reaches the caller's
          settlement and what ran is always a contiguous prefix; then
          iteration ends. The caller checks `stop_event`, as callers
          already do.
        - Closing the generator early cancels what has not started.
        - The time the caller spent waiting on the work is added to
          `stats[route]` — not the consumer's own time between results.
        """
        if self.n == 1:
            return self._inline(fn, items, stop_event)
        return self._threaded(fn, items, stop_event, window or 4 * self.n)

    def _inline(self, fn, items, stop_event):
        ops, waited = 0, 0.0
        try:
            for item in items:
                if stop_event is not None and stop_event.is_set():
                    return
                started = time.perf_counter()
                try:
                    result = fn(item)
                except Exception as e:
                    result = e
                waited += time.perf_counter() - started
                ops += 1
                yield item, result
        finally:
            self._stats.add(self._route, ops, waited)

    def _threaded(self, fn, items, stop_event, window):
        ops, waited = 0, 0.0
        pool = self._pool()
        source = iter(items)
        pending: deque = deque()

        def stopped() -> bool:
            return stop_event is not None and stop_event.is_set()

        def fill() -> None:
            while len(pending) < window and not stopped():
                try:
                    item = next(source)
                except StopIteration:
                    return
                pending.append((item, pool.submit(fn, item)))

        try:
            fill()
            # A stop is honoured by `fill`, which submits nothing more: what
            # was submitted (at most `window` items) finishes and is yielded,
            # in order. It is never cancelled — a worker takes an item off the
            # queue a moment before marking it running, so a cancel could
            # land on item k while k+1 already ran: work on disk behind a gap
            # the caller never settled (a stopped publish left files outside
            # its record). No cancel order closes that window; not cancelling
            # does, at the cost of at most `window` more operations.
            while pending:
                item, fut = pending.popleft()
                started = time.perf_counter()
                result = _outcome(fut)
                waited += time.perf_counter() - started
                ops += 1
                yield item, result
                fill()
        finally:
            for _item, fut in pending:
                fut.cancel()
            self._stats.add(self._route, ops, waited)


def _outcome(fut):
    """The future's result, or the Exception it raised. A BaseException
    (KeyboardInterrupt, SystemExit) is re-raised: it is not an item's
    result, it is the job ending."""
    try:
        return fut.result()
    except Exception as e:
        return e
