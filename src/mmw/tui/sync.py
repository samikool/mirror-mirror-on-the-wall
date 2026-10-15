import asyncio
import concurrent.futures
import threading
import time
from collections import deque

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical
from textual.markup import escape
from textual.message import Message
from textual.widgets import Label, ProgressBar, Static

from mmw.core import config
from mmw.core.format import (
    STALLED as _ETA_STALLED,
    fmt_eta as _fmt_eta,
    fmt_size as _fmt_size,
    fmt_speed as _fmt_speed,
)
from mmw.core.downloader import Status
from mmw.core.pool import Pool
from mmw.core.item_progress import ItemCounts, ItemInventory, ItemProgressSession
from mmw.core.download_coordinator import DownloadCoordinator
from mmw.core.sync_engine import KeySyncCoordinator, SyncEvents, classify_mirror, sync_mirror
from mmw.tui.ui.widgets import ThemedLog


# ── Messages ──────────────────────────────────────────────────────────────────

class MirrorStatus(Message):
    def __init__(self, mirror_name: str, status: str):
        self.mirror_name = mirror_name
        self.status = status
        super().__init__()


class MirrorStarted(Message):
    def __init__(self, mirror_name: str, total: int, label: str = "packages"):
        self.mirror_name = mirror_name
        self.total = total
        self.label = label
        super().__init__()


class MirrorFinished(Message):
    def __init__(self, mirror_name: str, downloaded: int, pooled: int, failed: int, elapsed: float,
                 metadata_ok: bool = True, metadata_errors: tuple[str, ...] = (), stopped: bool = False,
                 resnapshotted: bool = False):
        self.mirror_name = mirror_name
        self.downloaded = downloaded
        self.pooled = pooled
        self.failed = failed
        self.elapsed = elapsed
        self.metadata_ok = metadata_ok
        self.metadata_errors = metadata_errors
        self.resnapshotted = resnapshotted
        self.stopped = stopped
        super().__init__()


class MirrorError(Message):
    def __init__(self, mirror_name: str, error: str):
        self.mirror_name = mirror_name
        self.error = error
        super().__init__()


class MirrorDryRunResult(Message):
    def __init__(self, mirror_name: str, total: int, in_pool: int, to_fetch: int, fetch_size: int, has_unknown: bool,
                 inventory: ItemInventory | None = None):
        self.mirror_name = mirror_name
        self.total = total
        self.in_pool = in_pool
        self.to_fetch = to_fetch
        self.fetch_size = fetch_size
        self.has_unknown = has_unknown
        self.inventory = inventory
        super().__init__()


# ── Helpers ───────────────────────────────────────────────────────────────────

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# ── Mirror card ───────────────────────────────────────────────────────────────

class MirrorCard(Static, can_focus=True):

    BINDINGS = [
        Binding("up", "prev_card", show=False),
        Binding("down", "next_card", show=False),
        Binding("enter", "toggle_expand", "Expand/Collapse"),
        Binding("space", "toggle_expand", show=False),
    ]

    def __init__(self, mirror: config.MirrorConfig):
        super().__init__()
        self._mirror = mirror
        self._status_text = "Classifying packages..."  # shown until the first phase message
        self._total = 0
        self._item_label = "packages"
        self._logical_progress = False
        self._separate_transfer_events = False
        self._downloaded = 0
        self._pooled = 0
        self._skipped = 0
        self._failed = 0
        self._bytes = 0
        self._chunk_total = 0
        self._start: float | None = None
        self._done = False
        self._expanded = False
        # Live chunk bytes — written by thread pool workers, drained by 60 Hz tick
        self._chunk_bytes: int = 0
        self._chunk_lock = threading.Lock()
        self._speed_ema: float = 0.0
        self._speed_last_time: float = 0.0
        self._last_chunk_time: float = 0.0  # updated on every non-zero chunk batch
        # In-flight tracking — incremented by start_callback, decremented on completion
        self._in_flight: int = 0
        self._in_flight_lock = threading.Lock()
        # Package event log — written at completion rate, flushed to the log at 60 Hz
        self._log_pending: list[str] = []
        self._log_history: deque[str] = deque(maxlen=200)
        self._spinner_idx: int = 0
        # Pending counters — written at download rate, flushed to DOM at 60 Hz
        self._pending_downloaded: int = 0
        self._pending_pooled: int = 0
        self._pending_failed: int = 0
        self._pending_skipped: int = 0
        self._pending_bytes: int = 0
        self._pending_pkg: str = ""

    def compose(self) -> ComposeResult:
        yield Label(
            f"● {escape(self._mirror.name)}  [$secondary]{escape(self._mirror.provider)}[/]",
            classes="card-header",
        )
        yield Label("  Fetching metadata...", classes="card-waiting", id="waiting-msg")

    def start(self, total: int, label: str = "packages"):
        self._total = total
        self._item_label = label
        self._start = time.monotonic()
        try:
            self.query_one("#waiting-msg", Label).remove()
        except Exception:
            pass
        try:
            self.mount(ProgressBar(total=total, show_eta=False, id="pbar"))
            self.mount(Label("", classes="card-stats", id="stats"))
            self.mount(Label("", classes="card-pkg", id="pkg"))
        except Exception:
            pass
        self._refresh_stats(time.monotonic())

    def set_status(self, status: str):
        # Remember it: flush()'s spinner re-renders #pkg every tick until the
        # first package event, and must show the current phase, not a stale one.
        self._status_text = status
        try:
            self.query_one("#pkg", Label).update(f"  [dim]{escape(status)}[/]")
        except Exception:
            pass

    def add_chunk_bytes(self, n: int) -> None:
        """Called from thread pool workers per 64 KB chunk."""
        with self._chunk_lock:
            self._chunk_bytes += n      # drained by the speed EMA
            self._chunk_total += n      # cumulative, never drained
            if n:
                self._last_chunk_time = time.monotonic()

    def inc_in_flight(self) -> None:
        """Called from thread pool worker when a download actually starts."""
        with self._in_flight_lock:
            self._in_flight += 1

    def dec_in_flight(self) -> None:
        """A physical download ended, even if a fallback ultimately pools it."""
        with self._in_flight_lock:
            self._in_flight = max(0, self._in_flight - 1)

    @property
    def current_speed(self) -> float:
        return 0.0 if self._done else self._speed_ema

    def record_file(self, pkg_name: str, status: Status, bytes_xfr: int):
        """Transfer accounting stays per file, independent of item progress."""
        if not self._logical_progress:
            self.accumulate(pkg_name, status, bytes_xfr)
            return
        if status == Status.DOWNLOADED:
            self._pending_bytes += bytes_xfr
        if not self._separate_transfer_events and status in (Status.DOWNLOADED, Status.FAILED):
            with self._in_flight_lock:
                self._in_flight = max(0, self._in_flight - 1)
        if status == Status.FAILED and pkg_name:
            self._log_pending.append(f"  [$mmw-error]✗[/] {escape(pkg_name)} (file)")

    def accumulate(self, pkg_name: str, status: Status, bytes_xfr: int, *, track_transfer: bool = True):
        """Called at download rate — counters only, no DOM work."""
        if status == Status.DOWNLOADED:
            self._pending_downloaded += 1
            if track_transfer:
                self._pending_bytes += bytes_xfr
                with self._in_flight_lock:
                    self._in_flight = max(0, self._in_flight - 1)
            if pkg_name:
                self._log_pending.append(
                    f"  [$mmw-ok]↓[/] {escape(pkg_name)}  [dim]{_fmt_size(bytes_xfr)}[/]"
                )
        elif status == Status.POOLED:
            self._pending_pooled += 1
        elif status == Status.FAILED:
            self._pending_failed += 1
            if track_transfer:
                with self._in_flight_lock:
                    self._in_flight = max(0, self._in_flight - 1)
            if pkg_name:
                self._log_pending.append(f"  [$mmw-error]✗[/] {escape(pkg_name)}")
        elif status == Status.SKIPPED:
            self._pending_skipped += 1
        if pkg_name:
            self._pending_pkg = pkg_name

    def flush(self):
        """Called by _tick at 60 Hz — drains pending counters into the DOM."""
        self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER)
        spin = _SPINNER[self._spinner_idx]

        if self._start is None:
            try:
                self.query_one("#waiting-msg", Label).update(
                    f"  [dim]{spin} Fetching metadata…[/]"
                )
            except Exception:
                pass
            return

        now = time.monotonic()
        speed_updated = self._update_speed(now)

        n = self._pending_downloaded + self._pending_pooled + self._pending_failed + self._pending_skipped
        self._bytes += self._pending_bytes
        self._pending_bytes = 0
        if n > 0:
            self._downloaded += self._pending_downloaded
            self._pooled += self._pending_pooled
            self._skipped += self._pending_skipped
            self._failed += self._pending_failed
            last_pkg = self._pending_pkg
            self._pending_downloaded = self._pending_pooled = self._pending_failed = self._pending_skipped = self._pending_bytes = 0
            self._pending_pkg = ""
            try:
                self.query_one("#pbar", ProgressBar).advance(n)
                if last_pkg:
                    self.query_one("#pkg", Label).update(f"  → {escape(last_pkg)}")
            except Exception:
                pass

        # Drain log entries into history (always) and the log (if expanded)
        if self._log_pending:
            for entry in self._log_pending:
                self._log_history.append(entry)
            if self._expanded:
                try:
                    log = self.query_one("#pkg-log", ThemedLog)
                    for entry in self._log_pending:
                        log.write(entry)
                except Exception:
                    pass
            self._log_pending.clear()

        if self._expanded:
            try:
                with self._in_flight_lock:
                    n_flight = self._in_flight
                self.query_one("#in-flight", Label).update(
                    f"  [b $accent]↗ {n_flight}[/] in flight"
                )
            except Exception:
                pass

        # Started but no package events yet — spin over the current phase text
        # (set_status), so "Checking pool…" / "Linking N pooled packages…"
        # actually survive the 60 Hz re-render instead of being stomped.
        no_progress = (
            self._downloaded + self._pooled + self._skipped + self._failed +
            self._pending_downloaded + self._pending_pooled + self._pending_skipped + self._pending_failed
        ) == 0
        if no_progress and not self._done:
            try:
                self.query_one("#pkg", Label).update(f"  [dim]{spin} {escape(self._status_text)}[/]")
            except Exception:
                pass

        # Re-render the stats line only when something it shows actually moved —
        # a new completion, or the speed EMA (which also drives stall detection,
        # so this still catches a mirror going quiet). Re-rendering unconditionally
        # at 60 Hz per card was pure work, same as the footer's own throttle above.
        if n > 0 or speed_updated:
            self._refresh_stats(now)

    def finish(self, metadata_ok: bool = True, metadata_errors: tuple[str, ...] = (),
               stopped: bool = False, resnapshotted: bool = False):
        # `_tick` skips flushing done cards, so anything still pending here
        # would never reach the counters — the last packets of every mirror.
        try:
            self.flush()
        except Exception:
            pass
        self._done = True
        elapsed = time.monotonic() - (self._start or time.monotonic())
        for err in metadata_errors:
            self._log_history.append(f"  [$mmw-warn]⚠ {escape(err)}[/]")
        if resnapshotted and metadata_ok:
            # Upstream republished while packages were downloading and we picked
            # up the newer index — normal operation, not something to alarm about.
            self._log_history.append(
                "  [dim]· upstream republished during sync — re-read the index and used the newer one[/]"
            )
        if stopped:
            self.add_class("warn")
            line = f"  ⚠ Stopped after {_fmt_eta(elapsed)}  —  {_fmt_size(self._bytes)} transferred"
        elif self._failed:
            self.add_class("error")
            line = (f"  ✗ Failed {escape(self._item_label)}: {self._failed:,}"
                    f"  —  {_fmt_size(self._bytes)} transferred  (expand for detail)")
        elif not metadata_ok:
            self.add_class("warn")
            line = (
                f"  ⚠ {escape(self._item_label.capitalize())} synced, metadata NOT updated"
                f"  —  {_fmt_size(self._bytes)} transferred  (expand for detail)"
            )
        else:
            self.add_class("done")
            line = f"  ✓ Complete in {_fmt_eta(elapsed)}  —  {_fmt_size(self._bytes)} transferred"
        try:
            self.query_one("#pkg", Label).update(line)
        except Exception:
            pass

    def show_dry_run(self, total: int, in_pool: int, to_fetch: int, fetch_size: int, has_unknown: bool):
        self._done = True
        try:
            self.query_one("#waiting-msg", Label).remove()
        except Exception:
            pass
        pct_pool  = f"{in_pool  / total * 100:.0f}%" if total else "0%"
        pct_fetch = f"{to_fetch / total * 100:.0f}%" if total else "0%"
        size_str  = _fmt_size(fetch_size) if not has_unknown else f"~{_fmt_size(fetch_size)}+"
        unknown_note = "  [$mmw-warn]⚠ some sizes unknown[/]" if has_unknown else ""
        try:
            self.mount(Label(
                f"  [dim]{total:,} {escape(self._item_label)}[/]"
                f"  [$secondary]= {in_pool:,}[/] in pool ({pct_pool})"
                f"  [$mmw-ok]↓ {to_fetch:,}[/] to fetch ({pct_fetch})"
                f"  [bold]{size_str}[/]"
                + unknown_note,
                classes="card-stats",
            ))
        except Exception:
            pass
        self.add_class("done")

    def mark_error(self, error: str):
        self._done = True
        self.add_class("error")
        # Escape markup characters so error text renders literally
        safe = escape(error)
        entry = f"  [$mmw-error]✗ {safe}[/]"
        self._log_history.append(entry)
        for widget_id in ("#waiting-msg", "#pkg"):
            try:
                self.query_one(widget_id, Label).update(entry)
                break
            except Exception:
                continue
        if self._expanded:
            try:
                self.query_one("#pkg-log", ThemedLog).write(entry)
            except Exception:
                pass

    async def on_click(self) -> None:
        await self.action_toggle_expand()

    def action_prev_card(self) -> None:
        cards = list(self.app.query(MirrorCard))
        if cards and cards[0] is not self:
            self.app.action_focus_previous()

    def action_next_card(self) -> None:
        cards = list(self.app.query(MirrorCard))
        if cards and cards[-1] is not self:
            self.app.action_focus_next()

    async def action_toggle_expand(self) -> None:
        if self._expanded:
            self._expanded = False
            try:
                self.query_one("#expand-panel").remove()
            except Exception:
                pass
        else:
            self._expanded = True
            log = ThemedLog(id="pkg-log", classes="card-log", highlight=False, markup=True, wrap=False, max_lines=500)
            log.can_focus = False
            panel = Vertical(
                Label("  ↗ 0 in flight", id="in-flight", classes="card-inflight"),
                log,
                id="expand-panel",
            )
            await self.mount(panel)
            for entry in self._log_history:
                log.write(entry)

    _SPEED_INTERVAL = 0.25

    def _update_speed(self, now: float) -> bool:
        """Returns True if the EMA was recomputed (i.e. `_SPEED_INTERVAL` elapsed)."""
        dt = now - self._speed_last_time
        if dt < self._SPEED_INTERVAL:
            return False
        with self._chunk_lock:
            new_bytes = self._chunk_bytes
            self._chunk_bytes = 0
        self._speed_last_time = now
        instant = new_bytes / dt
        α = 0.3
        self._speed_ema = α * instant + (1 - α) * self._speed_ema
        return True

    _STALL_TIMEOUT = 30.0

    def _refresh_stats(self, now: float):
        completed = self._downloaded + self._pooled + self._skipped + self._failed
        speed = self._speed_ema

        warmup = max(10, self._total // 50)
        with self._in_flight_lock:
            in_flight = self._in_flight
        # An idle unit may be waiting on another unit's shared download. Its
        # old chunk timestamp is not evidence that a network request stalled.
        stalled = in_flight > 0 and self._last_chunk_time > 0 and (now - self._last_chunk_time) > self._STALL_TIMEOUT
        if stalled:
            eta = _ETA_STALLED
        elif completed >= warmup and self._start:
            elapsed = now - self._start
            remaining = self._total - completed
            eta = (elapsed / completed) * remaining
        else:
            eta = 0

        already = self._pooled + self._skipped
        stats = (
            f"  [dim]{completed:,}/{self._total:,} {escape(self._item_label)}[/]"
            f"  [$mmw-ok]↓ {self._downloaded:,}[/] downloaded"
            f"  [$secondary]= {already:,}[/] pooled"
            f"  [$mmw-error]✗ {self._failed}[/] failed"
            f"  [dim]•[/]  [bold]{_fmt_speed(speed)}[/]"
            f"  [dim]ETA {_fmt_eta(eta)}[/]"
        )
        try:
            self.query_one("#stats", Label).update(stats)
        except Exception:
            pass

    @property
    def bytes_transferred(self) -> int:
        """Bytes actually off the wire, counted per 64 KB chunk.

        **Not** the sum of completed packages, which is what this used to
        report: a mirror pulling multi-gigabyte CUDA packages completes one
        every few minutes, so the figure sat still while the speed display kept
        moving — indistinguishable from a frozen UI. Counting chunks also makes
        the two consistent, since speed was already chunk-based.

        Falls back to the completed-package total if no chunk callback was
        wired (nothing downloads in a dry run).
        """
        with self._chunk_lock:
            total = self._chunk_total
        return total or self._bytes


# ── Footer stats bar ──────────────────────────────────────────────────────────

class StatsFooter(Static):

    def __init__(self):
        super().__init__("")

    def refresh_dry_run(self, cards: list["MirrorCard"], total: int, in_pool: int,
                        to_fetch: int, fetch_size: int, label: str = "packages"):
        done = sum(1 for c in cards if c._done)
        n_cards = len(cards)
        self.update(
            f"[bold]Dry Run[/]  {done}/{n_cards} mirrors checked  [dim]│[/]  "
            f"[bold]{escape(label.capitalize())}[/]  {total:,} total  "
            f"[$secondary]{in_pool:,}[/] in pool  "
            f"[$mmw-ok]{to_fetch:,}[/] to fetch  "
            f"[bold]{_fmt_size(fetch_size)}[/] estimated"
        )

    def refresh_stats(self, stats: dict, cards: list[MirrorCard], items: list[ItemCounts] | None = None):
        """Render the footer from an already-fetched snapshot.

        **This must never query the pool.** `pool.stats()` is a full table scan
        measured at 9-14 s on a real pool; running it here — on the Textual
        event loop, from a 60 Hz timer — froze the entire UI. `SyncView` fetches
        it on a thread and hands the result in.
        """
        total_bytes = sum(c.bytes_transferred for c in cards)
        speed = sum(c.current_speed for c in cards)
        item_text = "".join(f"[bold]{escape(c.label.capitalize())}[/]  {c.completed:,}/{c.total:,}"
                            + (f" ({c.failed:,} failed)" if c.failed else "") + "  [dim]│[/]  "
                            for c in items or [])

        self.update(
            item_text + f"[bold]Pool[/]  {stats['packages']:,} files  "
            f"stored {_fmt_size(stats['total_size'])}  [dim]│[/]  "
            f"[bold]Transferred[/]  {_fmt_size(total_bytes)}  "
            f"@ {_fmt_speed(speed)}  [dim]│[/]  "
            f"[bold]Mirrors[/]  {sum(1 for c in cards if c._done)}/{len(cards)} done"
        )


# ── Main sync app ─────────────────────────────────────────────────────────────

class SyncView(Vertical):
    """One sync session: the cards, the footer, and everything that drives
    them — pool, executor, download semaphore, key coordinator, stop event,
    the pool-stats scan and the 60 Hz tick.

    The session lives inside MmwApp's Sync section (where it keeps running
    while other sections are shown), whether started from Repositories or by
    `mmw sync`. Posts `Completed` when every card is done.
    """

    class Completed(Message):
        def __init__(self, view: "SyncView"):
            self.view = view
            super().__init__()

    def __init__(self, cfg: config.Config, mirrors: list[config.MirrorConfig] | None = None,
                 dry_run: bool = False):
        super().__init__(id="sync-view")
        self._cfg = cfg
        self._mirrors = sorted(mirrors if mirrors is not None else cfg.mirrors,
                               key=lambda m: m.name.lower())
        self._pool: Pool | None = None
        self._cards: dict[str, MirrorCard] = {}
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._stop_event = threading.Event()
        # Set while nothing is draining: cleared by stop(), set again once the
        # cleanup thread has waited out the workers and closed the pool.
        self._drained = threading.Event()
        self._drained.set()
        self._last_footer_update = 0.0
        # Snapshot of the last completed pool scan, plus its guards. The scan
        # runs on a thread; the footer only ever renders what landed here.
        self._pool_stats: dict = {"packages": 0, "total_size": 0}
        self._pool_stats_at: float = 0.0
        self._pool_stats_inflight: bool = False
        self._sem: asyncio.Semaphore | None = None
        self._dry_run = dry_run
        self._items = ItemProgressSession(self._mirrors)
        self._downloads = DownloadCoordinator()
        self._complete_shown = False
        # The end-of-run pool scan: "" (not yet), "queued" (waiting for an
        # earlier scan to land) or "running"; `_final_scan_done` once the
        # footer shows it and the tick has stopped.
        self._final_scan = ""
        self._final_scan_done = False
        # dry-run aggregate totals (accumulated as results arrive)
        self._dr_total = 0
        self._dr_in_pool = 0
        self._dr_to_fetch = 0
        self._dr_fetch_size = 0

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    @property
    def running(self) -> bool:
        """True while any mirror is still syncing and nobody has stopped it."""
        return (not self._stop_event.is_set()
                and bool(self._cards) and not all(c._done for c in self._cards.values()))

    @property
    def draining(self) -> bool:
        """Stopped, but workers may still be finishing and the pool is open."""
        return not self._drained.is_set()

    def progress(self) -> tuple[int, int]:
        """(mirrors done, mirrors total)."""
        return sum(1 for c in self._cards.values() if c._done), len(self._cards)

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="cards"):
            for mirror in self._mirrors:
                card = MirrorCard(mirror)
                self._cards[mirror.name] = card
                yield card
        yield StatsFooter()

    def on_mount(self) -> None:
        # Everything here is allocation, not I/O: the executor creates its
        # threads lazily, the semaphore is an object, and KeySyncCoordinator
        # just holds a path and a lock.
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self._cfg.settings.max_connections
        )
        # Shared across all download_mirror calls: at most max_connections
        # package downloads in flight globally, competed for fairly.
        self._sem = asyncio.Semaphore(self._cfg.settings.max_connections)
        self._keys_coord = KeySyncCoordinator(self._cfg.settings.mirror_dir)
        self._ticker = self.set_interval(1 / 60, self._tick)
        # Opening the pool is disk work (mkdir, SQLite connect, schema script,
        # `_reap_stale_tmp()`), so it runs on a thread; `_tick` tolerates
        # `self._pool` being None, and no mirror starts before the pool exists.
        self._open_pool_and_start()
        try:
            self.app.active_sync = self
        except Exception:
            pass

    @work(exit_on_error=False)
    async def _open_pool_and_start(self) -> None:
        mirror_dir = self._cfg.settings.mirror_dir

        def _open() -> Pool:
            mirror_dir.mkdir(parents=True, exist_ok=True)
            return Pool(mirror_dir)

        try:
            pool = await asyncio.to_thread(_open)
        except Exception as e:
            # Previously this raised straight out of `on_mount` and killed the
            # app with a traceback. An unwritable mirror_dir (an NFS mount that
            # is not up, the wrong uid) is the most ordinary misconfiguration
            # there is, and it deserves a sentence.
            self.notify(f"Could not open the pool at {mirror_dir}: {e}",
                        severity="error", timeout=20)
            # No mirror can start without the pool — say so on every card, so
            # the session completes instead of sitting on "Fetching metadata…".
            for card in self._cards.values():
                card.mark_error(f"could not open the pool: {e}")
            self._check_all_done()
            return
        if self._stop_event.is_set():
            # Unmounted while the pool was opening — nothing will ever use it,
            # and `on_unmount` has already captured a None and moved on, so
            # closing it here is the only thing that will.
            await asyncio.to_thread(pool.close)
            # Stopped before any mirror started: finish the cards as stopped,
            # or the session never completes ("Stopping…" forever).
            for card in self._cards.values():
                if not card._done:
                    card.finish(stopped=True)
            self._check_all_done()
            return
        self._pool = pool
        for mirror in self._mirrors:
            self._sync_mirror(mirror)

    def stop(self) -> None:
        """Stop the session. Idempotent.

        Workers check the stop event per 64 KB chunk, but may still be inside
        pool.add — closing the DB under them raises in the worker. So cleanup
        goes to a daemon thread: wait for workers, then close the pool, without
        blocking on a stalled socket."""
        self._stop_event.set()
        executor, pool = self._executor, self._pool
        self._executor = self._pool = None

        def _cleanup():
            try:
                if executor:
                    executor.shutdown(wait=True, cancel_futures=True)
                if pool:
                    pool.close()
            finally:
                self._drained.set()
        if executor or pool:
            self._drained.clear()
            threading.Thread(target=_cleanup, daemon=True, name="sync-cleanup").start()

    def on_unmount(self) -> None:
        self.stop()
        try:
            if getattr(self.app, "active_sync", None) is self:
                self.app.active_sync = None
        except Exception:
            pass

    def on_descendant_focus(self, event) -> None:
        if isinstance(event.widget, MirrorCard):
            try:
                self.query_one("#cards", ScrollableContainer).scroll_to_widget(
                    event.widget, animate=True)
            except Exception:
                pass

    _FOOTER_HZ = 10.0
    # Measured from **completion**, not from the start of the previous call.
    # The old code stamped the clock before a 9-14 s scan, so by the time it
    # returned the 1 Hz gate was already 13 s stale and the next tick ran
    # another scan immediately — back-to-back full table scans with no gap,
    # holding the pool lock the 32 download workers were waiting on. The sync
    # made no network progress and the UI never repainted.
    _POOL_STATS_INTERVAL = 5.0

    def _maybe_refresh_pool_stats(self, now: float) -> None:
        if self._pool_stats_inflight:
            return
        if now - self._pool_stats_at < self._POOL_STATS_INTERVAL:
            return
        self._pool_stats_inflight = True
        self._pool_stats_worker()

    @work(thread=True, exit_on_error=False, group="sync-pool-stats")
    def _pool_stats_worker(self) -> None:
        """Full pool scan, off the event loop and off the pool's write lock."""
        try:
            stats = self._pool.stats() if self._pool else None
        except Exception:
            stats = None
        try:
            self.app.call_from_thread(self._on_pool_stats, stats)
        except Exception:
            # App is shutting down — release the guard so nothing wedges.
            self._pool_stats_inflight = False

    def _on_pool_stats(self, stats: "dict | None") -> None:
        if stats is not None:
            self._pool_stats = stats
        self._pool_stats_at = time.monotonic()   # spaced from completion
        self._pool_stats_inflight = False
        if self._final_scan == "running":
            self._render_footer()
            self._end_ticker()
        elif self._final_scan == "queued":
            self._final_scan = "running"
            self._pool_stats_inflight = True
            self._pool_stats_worker()

    def _tick(self):
        now = time.monotonic()
        for card in self._cards.values():
            try:
                if not card._done:
                    card.flush()
            except Exception:
                pass
        # The footer aggregates values that move at most a few times a second
        # (the pool query is already capped at 1 Hz, the speed EMA at 4 Hz), so
        # re-rendering it on every 60 Hz tick is pure work.
        if now - self._last_footer_update < 1.0 / self._FOOTER_HZ:
            return
        self._last_footer_update = now
        # Outside the try below: if scheduling the scan ever raises, the footer
        # must still render. Sharing one try meant a failure here silently froze
        # every number on screen with no error anywhere.
        if self._pool and not self._dry_run:
            try:
                self._maybe_refresh_pool_stats(now)
            except Exception:
                self._pool_stats_inflight = False
        self._render_footer()

    def _render_footer(self) -> None:
        try:
            footer = self.query_one(StatsFooter)
            cards = list(self._cards.values())
            if self._dry_run:
                footer.refresh_dry_run(
                    cards, self._dr_total, self._dr_in_pool, self._dr_to_fetch, self._dr_fetch_size,
                    self._item_label()
                )
            elif self._pool:
                footer.refresh_stats(self._pool_stats, cards, self._items.snapshot())
        except Exception:
            pass

    @on(MirrorStatus)
    def on_mirror_status(self, msg: MirrorStatus):
        if card := self._cards.get(msg.mirror_name):
            card.set_status(msg.status)

    @on(MirrorStarted)
    def on_mirror_started(self, msg: MirrorStarted):
        if card := self._cards.get(msg.mirror_name):
            card.start(msg.total, msg.label)

    @on(MirrorFinished)
    def on_mirror_finished(self, msg: MirrorFinished):
        if card := self._cards.get(msg.mirror_name):
            card.finish(metadata_ok=msg.metadata_ok, metadata_errors=msg.metadata_errors,
                        stopped=msg.stopped, resnapshotted=msg.resnapshotted)
        self._check_all_done()

    @on(MirrorError)
    def on_mirror_error(self, msg: MirrorError):
        if card := self._cards.get(msg.mirror_name):
            card.mark_error(msg.error)
        self._check_all_done()

    @on(MirrorDryRunResult)
    async def on_mirror_dry_run_result(self, msg: MirrorDryRunResult):
        self._dr_total      += msg.total
        self._dr_in_pool    += msg.in_pool
        self._dr_to_fetch   += msg.to_fetch
        self._dr_fetch_size += msg.fetch_size
        if msg.inventory is not None:
            await self._items.classify_async(msg.mirror_name, msg.inventory)
            counts = self._items.snapshot()
            self._dr_total = sum(c.total for c in counts)
            self._dr_in_pool = sum(c.pooled for c in counts)
            self._dr_to_fetch = sum(c.downloaded for c in counts)
        if card := self._cards.get(msg.mirror_name):
            card._item_label = msg.inventory.label if msg.inventory else "packages"
            card.show_dry_run(msg.total, msg.in_pool, msg.to_fetch, msg.fetch_size, msg.has_unknown)
        self._check_all_done()

    def _check_all_done(self):
        if self._complete_shown:
            return
        if not all(c._done for c in self._cards.values()):
            return
        self._complete_shown = True
        try:
            self.query_one(StatsFooter).add_class("complete")
        except Exception:
            pass                # unmounted while finishing (the app is exiting)
        self.post_message(self.Completed(self))
        self._finish_ticking()

    def _finish_ticking(self) -> None:
        """Nothing moves once every card is done, so the 60 Hz tick stops —
        but only after one last pool scan has reached the footer: its totals
        are the run's final word, and the 5 s scan cadence can leave them
        stale by a whole interval."""
        if self._pool is None or self._dry_run:
            self._render_footer()
            self._end_ticker()
            return
        # The final scan must *start* after the end; one already in flight
        # began before it, so the final one follows when that lands.
        self._final_scan = "queued" if self._pool_stats_inflight else "running"
        if self._final_scan == "running":
            self._pool_stats_inflight = True
            self._pool_stats_worker()       # lands in _on_pool_stats

    def _end_ticker(self) -> None:
        self._final_scan_done = True
        self._ticker.stop()

    def _item_label(self) -> str:
        return self._items.totals.label

    @property
    def item_failures(self) -> int:
        return self._items.totals.failed or sum(card._failed for card in self._cards.values())

    def summary(self) -> str:
        """One line for the hosting page once every mirror is done."""
        if not self._dry_run:
            done, total = self.progress()
            if self._stop_event.is_set():
                return f"Sync stopped — {done}/{total} mirrors finished or stopped."
            if self.item_failures:
                return f"Sync finished — {done}/{total} mirrors; failed {self._item_label()}: {self.item_failures:,}."
            return f"Sync complete — {done}/{total} mirrors."
        if not self._dr_total:
            return "Dry run complete."
        pct_pool = f"{self._dr_in_pool / self._dr_total * 100:.0f}%"
        pct_fetch = f"{self._dr_to_fetch / self._dr_total * 100:.0f}%"
        return (f"Dry run complete — {self._dr_total:,} {self._item_label()}: {self._dr_in_pool:,} in pool "
                f"({pct_pool}), {self._dr_to_fetch:,} to download ({pct_fetch}), "
                f"{_fmt_size(self._dr_fetch_size)} estimated.")

    @work(exit_on_error=False)
    async def _sync_mirror(self, mirror: config.MirrorConfig):
        """Presentation wrapper around the shared sync engine.

        Concurrency: every mirror starts immediately (index fetches are bounded
        by the executor's worker count); actual package downloads compete fairly
        across mirrors via the shared self._sem inside download_mirror.
        """
        start = time.monotonic()

        if self._dry_run:
            try:
                stats = await classify_mirror(mirror, self._pool, self._executor)
            except Exception as e:
                self.post_message(MirrorError(mirror.name, str(e) or type(e).__name__))
                return
            self.post_message(MirrorDryRunResult(
                mirror_name=mirror.name,
                total=stats.total,
                in_pool=stats.in_pool,
                to_fetch=stats.to_fetch,
                fetch_size=stats.fetch_size,
                has_unknown=stats.has_unknown,
                inventory=stats.inventory,
            ))
            return

        card = self._cards.get(mirror.name)

        def _on_started(total: int) -> None:
            # Phase text ("Checking pool…", "Linking…") follows from the engine.
            self.post_message(MirrorStarted(mirror.name, total))

        def _on_items_started(inventory: ItemInventory) -> None:
            if card:
                card._logical_progress = True
                card._separate_transfer_events = True
            self.post_message(MirrorStarted(mirror.name, len(inventory.ids), inventory.label))

        events = SyncEvents(
            phase=lambda text: self.post_message(MirrorStatus(mirror.name, text)),
            started=_on_started,
            progress=(lambda pkg, res: card.record_file(pkg, res.status, res.bytes_transferred)) if card else None,
            chunk=card.add_chunk_bytes if card else None,
            download_started=card.inc_in_flight if card else None,
            download_finished=card.dec_in_flight if card else None,
            items_started=_on_items_started,
            item_progress=(lambda event: card.accumulate(event.name, event.result.status,
                                                       event.result.bytes_transferred, track_transfer=False)) if card else None,
        )

        try:
            summary = await sync_mirror(
                mirror,
                pool=self._pool,
                mirror_dir=self._cfg.settings.mirror_dir,
                max_connections=self._cfg.settings.max_connections,
                executor=self._executor,
                events=events,
                sem=self._sem,
                stop_event=self._stop_event,
                keys=self._keys_coord,
                items_session=self._items,
                downloads=self._downloads,
            )
        except Exception as e:
            self.post_message(MirrorError(mirror.name, str(e) or type(e).__name__))
            return

        if summary.key_errors and card:
            for f in summary.key_errors:
                card._log_history.append(f"  [$mmw-warn]⚠ GPG key: {escape(f)}[/]")
        if card and summary.metadata_ok and not summary.stopped:
            card._log_pending.append(f"  [dim]· pool reference commit: {summary.reconcile_seconds:.2f}s[/]")

        elapsed = time.monotonic() - start
        self.post_message(MirrorFinished(
            mirror_name=mirror.name,
            downloaded=summary.downloaded,
            pooled=summary.pooled,
            failed=summary.failed,
            elapsed=elapsed,
            metadata_ok=summary.metadata_ok,
            metadata_errors=tuple(summary.metadata_errors),
            resnapshotted=summary.metadata_resnapshotted,
            stopped=summary.stopped,
        ))
