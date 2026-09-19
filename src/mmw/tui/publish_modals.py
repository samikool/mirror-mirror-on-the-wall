"""Publish-pipeline modals: dry-run report, live progress, prepare phases, certs."""
import threading
import time
from pathlib import Path

from rich.markup import escape as _escape
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Label, ProgressBar, RichLog, Static

from mmw.core.format import (
    fmt_duration as _fmt_duration,
    fmt_size as _fmt_bytes,
    fmt_speed as _fmt_speed,
)
from mmw.tui.widgets import Button


class DryRunResultModal(ModalScreen[bool]):
    """Read-only publish report — shows sizes and diff, no filesystem writes."""

    DEFAULT_CSS = """
    DryRunResultModal { align: center middle; }
    DryRunResultModal > Vertical {
        width: 90%;
        max-width: 130;
        height: auto;
        max-height: 85%;
        border: double $primary;
        background: $surface;
        padding: 1 2;
    }
    DryRunResultModal .modal-title { text-style: bold; color: $primary; margin-bottom: 1; }
    DryRunResultModal .error-row   { color: $error; }
    DryRunResultModal .totals      { color: $primary; margin-top: 1; text-style: bold; }
    DryRunResultModal .du-hdr      { color: $accent; text-style: bold; margin-top: 1; }
    DryRunResultModal .du-path     { color: $text; margin-left: 2; }
    DryRunResultModal .du-warn     { color: $warning; margin-left: 2; }
    DryRunResultModal .du-empty    { color: $text-muted; margin-left: 2; }
    DryRunResultModal DataTable    { height: auto; max-height: 24; margin-bottom: 1; }
    DryRunResultModal Horizontal   { align: center middle; height: auto; margin-top: 1; }
    DryRunResultModal Button       { margin: 0 1; }
    """

    def __init__(self, network_name: str, result):
        super().__init__()
        self._network_name = network_name
        self._result = result

    def compose(self) -> ComposeResult:
        r = self._result
        fmt = _fmt_bytes
        with Vertical():
            yield Label(f"Publish dry run — {self._network_name}", classes="modal-title")
            yield DataTable(show_cursor=False, zebra_stripes=True, id="dry-run-table")
            yield Label(
                f"  Total  {r.total_files:,} files  ·  {fmt(r.total_bytes)}"
                f"  →  {fmt(r.skipped_bytes)} already published"
                f"  ·  ≈{fmt(r.linked_bytes)} to add (estimated from index diff)",
                classes="totals",
            )
            if r.errors:
                yield Label(f"  {len(r.errors)} error(s) — see rows marked ✗", classes="error-row")
            if r.dist_upgrade_paths or r.dist_upgrade_warnings:
                yield Label("  Dist-Upgrade", classes="du-hdr")
                if r.dist_upgrade_paths:
                    for from_s, to_s in r.dist_upgrade_paths:
                        yield Label(f"  ✓  {from_s} → {to_s}", classes="du-path")
                else:
                    yield Label("  No consecutive LTS pairs found in this network", classes="du-empty")
                for w in r.dist_upgrade_warnings:
                    yield Label(f"  ⚠  {w}", classes="du-warn")
            with Horizontal():
                yield Button("Close", variant="default", id="cancel")
                yield Button("Publish Now", variant="primary", id="proceed")

    def on_mount(self) -> None:
        r = self._result
        fmt = _fmt_bytes
        table = self.query_one("#dry-run-table", DataTable)
        table.add_columns("Mirror", "Files", "Total", "Published", "To Add (est.)")
        for mr in r.mirrors:
            if mr.errors:
                table.add_row(mr.mirror_name, "—", "—", "—", f"✗ {mr.errors[0]}")
            else:
                table.add_row(
                    mr.mirror_name,
                    f"{mr.total_files:,}",
                    fmt(mr.total_bytes),
                    fmt(mr.skipped_bytes),
                    fmt(mr.linked_bytes),
                )

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#proceed")
    def _proceed(self) -> None:
        self.dismiss(True)

def _transfer_progress_css(cls_name: str, prefix: str) -> str:
    """CSS for a `_TransferProgressModal` subclass, parameterized by its own
    class name (Textual CSS type selectors match by name, not by base class)
    and its `ID_PREFIX`. Generated rather than duplicated so the geometry the
    two variants share can never drift — see `_TransferProgressModal`'s
    docstring for why the duplicate this replaces was worth removing."""
    return f"""
    {cls_name} {{ align: center middle; }}
    {cls_name} > Vertical {{
        width: 95%;
        height: 90%;
        border: double $primary;
        background: $surface;
    }}
    {cls_name} #{prefix}-title-bar {{
        background: $primary-darken-3;
        padding: 0 2;
        height: 1;
        color: $text;
        text-style: bold;
    }}
    {cls_name} #{prefix}-progress-area {{
        height: auto;
        padding: 1 2 0 2;
    }}
    {cls_name} ProgressBar {{
        margin: 0 0 1 0;
    }}
    {cls_name} .{prefix}-section-label {{
        height: 1;
        color: $text-muted;
        text-style: bold;
    }}
    {cls_name} .{prefix}-mirror-label {{
        height: 1;
        color: $accent;
        margin-top: 1;
    }}
    {cls_name} .{prefix}-archive-rate-label {{
        height: 1;
        color: $accent;
    }}
    {cls_name} .{prefix}-archive-file-label {{
        height: 1;
        color: $warning;
    }}
    {cls_name} .{prefix}-stats {{
        height: 1;
        color: $text;
        margin-top: 1;
    }}
    {cls_name} .{prefix}-eta {{
        height: 1;
        color: $text-muted;
        margin-bottom: 1;
    }}
    {cls_name} #{prefix}-log {{
        height: 1fr;
        border-top: solid $primary-darken-2;
        background: $background;
    }}
    {cls_name} #{prefix}-controls {{
        height: auto;   /* +1 row for border-top; 3-row buttons need 3 content rows */
        align: right middle;
        padding: 0 2;
        background: $primary-darken-3;
        border-top: solid $primary-darken-1;
        dock: bottom;
    }}
    {cls_name} #{prefix}-footer-label {{
        width: 1fr;
        color: $text-muted;
        content-align: left middle;
    }}
    {cls_name} #btn-stop {{
        margin-left: 1;
    }}
    """


class _TransferProgressModal(ModalScreen):
    """Shared machinery behind `PublishProgressModal` and `BundleProgressModal`.

    `publish_group` (`core/publisher/links.py`) fires the identical
    `progress(rel, status)` calls into a `BundleSink` as it does into a
    `TreeSink` (`core/publisher/sinks.py`) — "linked"/"skipped"/"pruned"/
    "by-hash:N"/"enumerate-*"/"prune-scan-*" are the Sink protocol's own
    vocabulary, not publish-specific wording — so a live progress screen over
    either one is the same counters, the same lock-protected buffer drained by
    a 60 Hz `_tick`, and the same completion states. Extracted here rather
    than kept as two copies because this exact counter/tick/completion logic
    has already needed three separate one-at-a-time fixes (the
    flush-before-done bug, the try-block-scope bug, the rate-limit-timing
    bug) — a duplicate means every future fix like that has to be applied
    twice, with nothing enforcing that it will be.

    Subclasses supply wording (`VERB_ING`/`VERB_PAST`/`COUNT_LABEL`), the
    widget id prefix (`ID_PREFIX`), and whether a per-mirror progress bar
    renders (`SHOW_MIRROR_BAR`) — a bundle has no destination-scan file count
    to bound one against ahead of time, so it shows a running "N considered"
    count instead. Only `notify_mirror_start`/`record`/`_tick` read that flag,
    and each check is a single `if`, never an `isinstance` — if a future
    caller needs more than that, this base has stopped being a good fit and
    should not be stretched further to cover it.
    """

    BINDINGS = [Binding("escape", "action_close_or_stop", "Close/Stop")]

    ID_PREFIX = "xfer"
    VERB_ING = "Transferring"
    VERB_PAST = "Transferred"
    COUNT_LABEL = "done"
    SHOW_MIRROR_BAR = True

    def __init__(self, network_name: str, total: int = 0,
                 per_mirror_counts: dict[str, int] | None = None):
        super().__init__()
        self._network_name = network_name
        self._total = total
        self._per_mirror_counts = per_mirror_counts or {}
        self._start = time.monotonic()
        self._lock = threading.Lock()
        # Global counters
        self._linked = 0
        self._skipped = 0
        self._pruned = 0
        self._log_pending: list[str] = []
        # Per-mirror counters (reset each mirror)
        self._mirror_linked = 0
        self._mirror_skipped = 0
        self._mirror_name = ""
        self._mirror_index = 0
        self._mirror_total_count = 0
        self._mirror_total_mirrors = 0
        self._phase = ""
        self._pending_mirror_change: tuple | None = None
        # Speed
        self._speed_ema: float = 0.0
        self._last_speed_time: float = 0.0
        self._last_speed_linked: int = 0
        # Archive-writing phase (BundleSink.close()) — the actual I/O for a
        # bundle happens entirely here, invisible to the counters above,
        # which only track the fast in-memory enumeration phase. Totals
        # arrive via "archive-start" the instant they're known (before any
        # archive I/O runs), so the bar is determinate from the first tick.
        #
        # Raw and compressed bytes are tracked separately, never conflated:
        # `_archive_total_bytes` (from archive-start) is uncompressed source
        # size, and `_archive_raw_bytes_done` is comparable to it — an
        # apples-to-apples fraction that can reach a clean 100%. Comparing
        # `_archive_total_bytes` against the compressed bytes actually
        # written to disk was the bug this replaced: two different units
        # that could never cleanly reach 100% against each other. The
        # compressed figure is kept too — it drives the write-speed EMA
        # (the "how fast are we actually writing to disk" number) and the
        # live compression ratio, the way an archiving tool's progress
        # display would show both.
        self._archive_total_files: int = 0
        self._archive_total_bytes: int = 0
        self._archive_files_done: int = 0
        self._archive_raw_bytes_done: int = 0
        self._archive_compressed_bytes_done: int = 0
        self._archive_files_ema: float = 0.0
        self._archive_bps_ema: float = 0.0
        self._archive_last_sample_time: float = 0.0
        self._archive_last_sample_files: int = 0
        self._archive_last_sample_bytes: int = 0
        # A single large file's own progress (see BundleSink._ProgressFile) —
        # separate from the counters above, which only advance once a whole
        # file finishes. Rendered as an extra clause on the status line rather
        # than its own log lines, since it can update many times a second for
        # as long as one multi-GB file takes to compress. `_current_large_file`
        # empty means none in flight.
        self._current_large_file: str = ""
        self._current_large_file_done: int = 0
        self._current_large_file_size: int = 0
        self.stop_event = threading.Event()
        # Set by mark_done / mark_error from the transfer thread
        self._transfer_done: bool = False
        self._result = None
        self._error_msg: str = ""
        self._done_displayed: bool = False
        self._dismissed: bool = False

    def compose(self) -> ComposeResult:
        p = self.ID_PREFIX
        with Vertical():
            yield Static(f"  {self.VERB_ING}  {self._network_name}", id=f"{p}-title-bar")
            with Vertical(id=f"{p}-progress-area"):
                yield Label("  Overall", id=f"{p}-overall-label", classes=f"{p}-section-label")
                yield ProgressBar(
                    total=self._total if self._total else None,
                    show_eta=False,
                    id=f"{p}-pbar",
                )
                yield Label("  Waiting for first mirror…", id=f"{p}-mirror-label", classes=f"{p}-mirror-label")
                archive_rate_label = Label(
                    "", id=f"{p}-archive-rate-label", classes=f"{p}-archive-rate-label")
                archive_rate_label.display = False
                yield archive_rate_label
                archive_file_label = Label(
                    "", id=f"{p}-archive-file-label", classes=f"{p}-archive-file-label")
                archive_file_label.display = False
                yield archive_file_label
                if self.SHOW_MIRROR_BAR:
                    yield ProgressBar(total=None, show_eta=False, id=f"{p}-mirror-pbar")
                yield Label("  Starting…", id=f"{p}-stats", classes=f"{p}-stats")
                yield Label("", id=f"{p}-eta", classes=f"{p}-eta")
            log = RichLog(id=f"{p}-log", highlight=False, markup=True, wrap=False, max_lines=2000)
            log.can_focus = False
            yield log
            with Horizontal(id=f"{p}-controls"):
                yield Static("", id=f"{p}-footer-label")
                yield Button("Stop", id="btn-stop", variant="error")

    def on_mount(self) -> None:
        self._last_speed_time = time.monotonic()
        self.set_interval(1 / 60, self._tick)

    def _safe_dismiss(self, result) -> None:
        if not self._dismissed:
            self._dismissed = True
            self.dismiss(result)

    def action_close_or_stop(self) -> None:
        if self._done_displayed:
            self._safe_dismiss(self._result)
        elif not self.stop_event.is_set():
            self.stop_event.set()
            try:
                self.query_one("#btn-stop", Button).disabled = True
                self.query_one(f"#{self.ID_PREFIX}-title-bar", Static).update(
                    f"  Stopping  {self._network_name}  [dim](finishing current file…)[/]"
                )
            except Exception:
                pass

    @on(Button.Pressed, "#btn-stop")
    def _btn_stop_pressed(self) -> None:
        self.action_close_or_stop()

    def mark_done(self, result) -> None:
        """Called from the transfer thread (or its await) when it completes."""
        with self._lock:
            self._transfer_done = True
            self._result = result

    def mark_error(self, msg: str) -> None:
        """Called when the transfer thread raises an exception."""
        with self._lock:
            self._transfer_done = True
            self._error_msg = msg

    def notify_mirror_start(self, name: str, index: int, total_mirrors: int) -> None:
        """Called from the transfer thread when a new mirror/group begins."""
        file_count = self._per_mirror_counts.get(name, 0)
        with self._lock:
            self._pending_mirror_change = (name, index, total_mirrors, file_count)
            self._mirror_linked = 0
            self._mirror_skipped = 0

    def record(self, dst: str, status: str) -> None:
        """Called from the transfer thread — pure counter + buffer update, no DOM work.

        `dst` is a sink-relative POSIX string (never a `Path`) — see the Sink
        progress contract in `core/publisher/sinks.py`.
        """
        parts = dst.split("/")
        short = "/".join(parts[-4:]) if len(parts) >= 4 else dst
        with self._lock:
            if status == "linked":
                self._linked += 1
                self._mirror_linked += 1
                self._log_pending.append(f"  [green]→[/] {short}")
            elif status == "skipped":
                self._skipped += 1
                self._mirror_skipped += 1
            elif status in ("linked-meta", "skipped-meta"):
                pass  # metadata ops don't count toward package totals
            elif status == "pruned":
                self._pruned += 1
                self._log_pending.append(f"  [red]✂[/] {short}  [dim](pruned)[/]")
            elif status.startswith("by-hash:"):
                n = status.split(":", 1)[1]
                self._log_pending.append(f"  [dim]# by-hash links: {n}[/]")
            # Long silent phases — enumeration and the destination scan — report
            # here so a multi-minute walk over a big tree never reads as a hang.
            elif status == "enumerate-start":
                self._phase = "reading pool index…"
                self._log_pending.append("  [dim]# reading pool index…[/]")
            elif status.startswith("enumerate-done:"):
                n = int(status.split(":", 1)[1])
                self._phase = ""
                self._log_pending.append(f"  [dim]# {n:,} files to publish[/]")
            elif status == "prune-scan-start":
                self._phase = "scanning destination…"
                self._log_pending.append("  [dim]# scanning destination for stale files…[/]")
            elif status.startswith("prune-scan-progress:"):
                n = int(status.split(":", 1)[1])
                self._phase = f"scanning destination · {n:,} checked"
                self._log_pending.append(f"  [dim]# destination scan: {n:,} files checked…[/]")
            elif status.startswith("prune-scan-done:"):
                _, checked, removed = status.split(":")
                self._phase = ""
                self._log_pending.append(
                    f"  [dim]# destination scan done: {int(checked):,} checked, "
                    f"{int(removed):,} removed[/]"
                )
            # Archive-writing phase (BundleSink.close()) — see the __init__
            # comment above these fields. `link()`/`write()` only ever append
            # to in-memory lists, so these are the only status events that
            # correspond to real disk I/O for a bundle.
            elif status.startswith("archive-large-file:"):
                # Unthrottled and immediate, unlike the sampled per-file line
                # below — a single multi-hundred-MB/GB file (driver/kernel
                # packages this pool holds plenty of) can otherwise leave the
                # log silent for however long it takes to compress, reading
                # as a stall instead of real, in-progress work.
                size = int(status.split(":", 1)[1])
                self._current_large_file = dst
                self._current_large_file_done = 0
                self._current_large_file_size = size
                self._log_pending.append(
                    f"  [magenta]▶[/] compressing {short}  [dim]({_fmt_bytes(size)})[/]"
                )
            elif status.startswith("archive-large-file-progress:"):
                # No log line — this can fire many times a second for as long
                # as one big file takes; it updates the persistent status line
                # instead (see _tick()), the same way the mirror-label line
                # already reports without spamming the scrolling log.
                _, done, size = status.split(":")
                if dst == self._current_large_file:
                    self._current_large_file_done = int(done)
                    self._current_large_file_size = int(size)
            elif status.startswith("archive-start:"):
                _, total_files, total_bytes = status.split(":")
                self._archive_total_files = int(total_files)
                self._archive_total_bytes = int(total_bytes)
                self._log_pending.append(
                    f"  [dim]# writing archive: {int(total_files):,} files, "
                    f"{_fmt_bytes(int(total_bytes))}…[/]"
                )
            elif status.startswith("archive-progress:"):
                _, files_done, raw_done, compressed_done = status.split(":")
                self._archive_files_done = int(files_done)
                self._archive_raw_bytes_done = int(raw_done)
                self._archive_compressed_bytes_done = int(compressed_done)
                # `dst` is whichever file happened to complete the instant
                # this (throttled, see BundleSink._maybe_report_archive_
                # progress) report fired — not every file, the same way the
                # numeric counters above only sample rather than tick per
                # file. Enough to show real motion through the archive
                # without a second unthrottled log path into this widget.
                if dst:
                    self._log_pending.append(f"  [magenta]⇢[/] {short}")
                # This file has finished (that's what `archive-progress` fires
                # on) — if it was the large file currently shown as "in
                # progress", clear it so the status line stops claiming it's
                # still being worked on.
                if dst and dst == self._current_large_file:
                    self._current_large_file = ""
                    self._current_large_file_done = 0
                    self._current_large_file_size = 0
            elif status.startswith("archive-done:"):
                _, files_done, raw_done, compressed_done = status.split(":")
                self._archive_files_done = int(files_done)
                self._archive_raw_bytes_done = int(raw_done)
                self._archive_compressed_bytes_done = int(compressed_done)
                self._current_large_file = ""
                self._current_large_file_done = 0
                self._current_large_file_size = 0
                ratio = (int(raw_done) / int(compressed_done)) if int(compressed_done) else 0.0
                self._log_pending.append(
                    f"  [dim]# archive written: {int(files_done):,} files, "
                    f"{_fmt_bytes(int(compressed_done))} written "
                    f"({ratio:.1f}x from {_fmt_bytes(int(raw_done))})[/]"
                )

    def _tick(self) -> None:
        p = self.ID_PREFIX
        now = time.monotonic()
        with self._lock:
            linked = self._linked
            skipped = self._skipped
            pruned = self._pruned
            mirror_linked = self._mirror_linked
            mirror_skipped = self._mirror_skipped
            pending = self._log_pending[:]
            self._log_pending.clear()
            mirror_change = self._pending_mirror_change
            self._pending_mirror_change = None
            phase = self._phase
            archive_total_files = self._archive_total_files
            archive_total_bytes = self._archive_total_bytes
            archive_files_done = self._archive_files_done
            archive_raw_bytes_done = self._archive_raw_bytes_done
            archive_compressed_bytes_done = self._archive_compressed_bytes_done
            current_large_file = self._current_large_file
            current_large_file_done = self._current_large_file_done
            current_large_file_size = self._current_large_file_size

        total_done = linked + skipped

        # Apply mirror change
        if mirror_change:
            name, index, total_mirrors, file_count = mirror_change
            self._mirror_name = name
            self._mirror_index = index
            self._mirror_total_mirrors = total_mirrors
            self._mirror_total_count = file_count
            if self.SHOW_MIRROR_BAR:
                try:
                    pbar = self.query_one(f"#{p}-mirror-pbar", ProgressBar)
                    pbar.total = file_count if file_count else None
                    pbar.progress = 0
                except Exception:
                    pass

        # Speed EMA — files/s (linked + skipped), sampled every 250 ms
        dt = now - self._last_speed_time
        if dt >= 0.25:
            instant = (total_done - self._last_speed_linked) / dt
            self._speed_ema = 0.3 * instant + 0.7 * self._speed_ema
            self._last_speed_linked = total_done
            self._last_speed_time = now

        speed = self._speed_ema
        elapsed = now - self._start

        # Archive-writing phase EMA — files/s and bytes/s, sampled every
        # 250 ms exactly like the enumeration-phase speed above. Only ticks
        # once `archive-start` has fired (see record()). Bytes/s is deliberately
        # sampled from the *compressed* counter — "how fast are we actually
        # writing to disk" — not the raw/uncompressed one, which drives the
        # completion fraction instead (see the field comments in __init__).
        if archive_total_files:
            if self._archive_last_sample_time == 0.0:
                self._archive_last_sample_time = now
                self._archive_last_sample_files = archive_files_done
                self._archive_last_sample_bytes = archive_compressed_bytes_done
            adt = now - self._archive_last_sample_time
            if adt >= 0.25:
                inst_files = (archive_files_done - self._archive_last_sample_files) / adt
                inst_bytes = (archive_compressed_bytes_done - self._archive_last_sample_bytes) / adt
                self._archive_files_ema = 0.3 * inst_files + 0.7 * self._archive_files_ema
                self._archive_bps_ema = 0.3 * inst_bytes + 0.7 * self._archive_bps_ema
                self._archive_last_sample_time = now
                self._archive_last_sample_files = archive_files_done
                self._archive_last_sample_bytes = archive_compressed_bytes_done

        # Overall progress bar — once the archive-writing phase has started,
        # it repurposes this bar: the enumeration phase it tracked before
        # that is already over, and the archive's own totals are known and
        # determinate the moment `archive-start` fires. Bytes, not files: this
        # pool holds files from a few KB to 5+ GB, so a file-count fraction
        # would read as "almost done" the instant the many small files clear,
        # while the actual data volume remaining could still be most of it.
        try:
            if archive_total_files:
                bar = self.query_one(f"#{p}-pbar", ProgressBar)
                bar.total = archive_total_bytes if archive_total_bytes else None
                bar.progress = archive_raw_bytes_done
            elif self._total:
                self.query_one(f"#{p}-pbar", ProgressBar).progress = total_done
        except Exception:
            pass

        # Per-mirror progress bar — only when this variant renders one. Kept
        # as its own try block, separate from the label update below: the two
        # used to share one, so a variant with no bar widget at all raised on
        # the very first `query_one` and the label update below it never ran.
        if self.SHOW_MIRROR_BAR:
            try:
                mirror_done = mirror_linked + mirror_skipped
                pbar = self.query_one(f"#{p}-mirror-pbar", ProgressBar)
                if self._mirror_total_count:
                    pbar.progress = mirror_done
            except Exception:
                pass

        # Mirror label — repurposed for the archive-writing phase, which has
        # no mirror of its own (it runs once, after every group is done).
        #
        # The label's own CSS sets a single base color ($accent); markup like
        # [bold]/[dim] only ever adjusts weight or intensity of THAT color, so
        # without explicit color tags every segment renders as shades of one
        # hue. Each stat gets its own color here instead — matching the
        # palette record() already uses elsewhere (green for real progress,
        # cyan for rates, dim for secondary/contextual numbers) — so the four
        # different kinds of information (data progress, file count, speed,
        # compressed output) read as visually distinct at a glance.
        try:
            if archive_total_files:
                # Percentage is bytes-based (raw/uncompressed), same reasoning
                # as the progress bar above — file count alone is misleading
                # when a handful of multi-GB files dominate the actual work.
                pct = (f" [dim]({100 * archive_raw_bytes_done / archive_total_bytes:.0f}%)[/]"
                       if archive_total_bytes else "")
                ratio = (archive_raw_bytes_done / archive_compressed_bytes_done
                         if archive_compressed_bytes_done else 0.0)
                ratio_str = f" [dim]({ratio:.1f}x)[/]" if archive_compressed_bytes_done else ""
                # Split across three lines/widgets, not one long one: at
                # realistic scale (multi-TB, six-figure file counts) the
                # combined line ran past 130 columns before a filename was
                # even added — well past any terminal width worth assuming.
                self.query_one(f"#{p}-mirror-label", Label).update(
                    f"  [bold]Writing archive[/]"
                    f"  [dim]·[/]  [green]{_fmt_bytes(archive_raw_bytes_done)} / "
                    f"{_fmt_bytes(archive_total_bytes)}[/]{pct}"
                    f"  [dim]·[/]  [dim]{archive_files_done:,} / {archive_total_files:,} files[/]"
                )
                rate_label = self.query_one(f"#{p}-archive-rate-label", Label)
                rate_label.update(
                    f"  [cyan]{self._archive_files_ema:,.0f} files/s[/]"
                    f"  [dim]·[/]  [cyan]{_fmt_speed(self._archive_bps_ema)}[/]"
                    f"  [dim]·[/]  [magenta]{_fmt_bytes(archive_compressed_bytes_done)} written[/]{ratio_str}"
                )
                rate_label.display = True
                # One big file's own progress, when there is one in flight —
                # see BundleSink._ProgressFile. Filename only, on its own line.
                file_label = self.query_one(f"#{p}-archive-file-label", Label)
                if current_large_file and current_large_file_size:
                    file_pct = 100 * current_large_file_done / current_large_file_size
                    fname = current_large_file.rsplit("/", 1)[-1]
                    file_label.update(f"  [yellow]▶ compressing {fname}: {file_pct:.0f}%[/]")
                    file_label.display = True
                else:
                    file_label.display = False
            elif self._mirror_name:
                # Not in the archive-writing phase (or never entering it at
                # all — the common case for PublishProgressModal): the two
                # extra lines it uses must not linger from a previous tick.
                try:
                    self.query_one(f"#{p}-archive-rate-label", Label).display = False
                    self.query_one(f"#{p}-archive-file-label", Label).display = False
                except Exception:
                    pass
                mirror_done = mirror_linked + mirror_skipped
                if self._mirror_total_count:
                    pbar_pct = f"  [dim]{100 * mirror_done / self._mirror_total_count:.0f}%[/]"
                    count_str = f"  [dim]{mirror_done:,} / {self._mirror_total_count:,}[/]{pbar_pct}"
                elif self.SHOW_MIRROR_BAR:
                    count_str = ""
                else:
                    count_str = f"  [dim]{mirror_done:,} considered[/]"
                # The counters stall during enumeration and the destination
                # scan; the phase says which one is running.
                phase_str = f"  [dim]·[/]  [dim]{phase}[/]" if phase else ""
                self.query_one(f"#{p}-mirror-label", Label).update(
                    f"  Mirror {self._mirror_index} / {self._mirror_total_mirrors}"
                    f"  [dim]·[/]  [bold]{self._mirror_name}[/]"
                    f"{count_str}{phase_str}"
                )
        except Exception:
            pass

        pct = f"  [dim]{100 * total_done / self._total:.0f}%[/]" if self._total else ""
        total_str = f"{self._total:,}" if self._total else "?"

        pruned_str = f"  [red]✂ {pruned:,}[/] pruned" if pruned else ""
        try:
            self.query_one(f"#{p}-stats", Label).update(
                f"  [green]↗ {linked:,}[/] {self.COUNT_LABEL}"
                f"  [cyan]= {skipped:,}[/] already current"
                f"{pruned_str}"
                f"  [dim]·[/]  [bold]{speed:,.0f}[/] files/s"
                f"{pct}"
            )
            self.query_one(f"#{p}-eta", Label).update(
                f"  [dim]elapsed {_fmt_duration(elapsed)}"
                f"  ·  {total_done:,} / {total_str} files[/]"
            )
        except Exception:
            pass

        # Flush log entries into RichLog
        if pending:
            try:
                log = self.query_one(f"#{p}-log", RichLog)
                for entry in pending:
                    log.write(entry)
            except Exception:
                pass

        # Footer label
        try:
            stopping = "  [bold red]Stopping…[/]  " if self.stop_event.is_set() else ""
            self.query_one(f"#{p}-footer-label", Static).update(
                f"{stopping}[dim]{self._network_name}  ·  {_fmt_duration(elapsed)}[/]"
            )
        except Exception:
            pass

        # Transfer completion — runs once when thread signals done
        if self._transfer_done and not self._done_displayed:
            self._done_displayed = True
            with self._lock:
                result = self._result
                error = self._error_msg
            try:
                if error:
                    self.query_one(f"#{p}-title-bar", Static).update(
                        f"  [bold red]✗ Error[/]  {self._network_name}"
                    )
                    self.query_one(f"#{p}-log", RichLog).write(
                        f"  [red]✗ {_escape(error)}[/]"
                    )
                    btn = self.query_one("#btn-stop", Button)
                    btn.label = "Close"
                    btn.variant = "error"
                    btn.disabled = False
                elif self.stop_event.is_set():
                    self.query_one(f"#{p}-title-bar", Static).update(
                        f"  [bold yellow]⚠ Stopped[/]  {self._network_name}"
                    )
                    btn = self.query_one("#btn-stop", Button)
                    btn.label = "Close"
                    btn.variant = "warning"
                    btn.disabled = False
                else:
                    self.query_one(f"#{p}-title-bar", Static).update(
                        f"  [bold green]✓ {self.VERB_PAST}[/]  {self._network_name}"
                    )
                    btn = self.query_one("#btn-stop", Button)
                    btn.label = "Close"
                    btn.variant = "primary"
                    btn.disabled = False
            except Exception:
                pass
            # Auto-close after 4 s so user can read the result; they can also
            # click Close or press Escape to dismiss immediately.
            self.set_timer(4.0, lambda: self._safe_dismiss(result))


class PublishProgressModal(_TransferProgressModal):
    """Full-screen progress view shown while publish_network runs."""

    ID_PREFIX = "pub"
    VERB_ING = "Publishing"
    VERB_PAST = "Published"
    COUNT_LABEL = "linked"
    SHOW_MIRROR_BAR = True

    DEFAULT_CSS = _transfer_progress_css("PublishProgressModal", "pub")


class BundleProgressModal(_TransferProgressModal):
    """Full-screen progress view shown while bundle_network runs.

    Same pipeline as `PublishProgressModal` — `publish_group` fires the same
    `progress(rel, status)` calls into a `BundleSink` as it does into a
    `TreeSink` — so this differs from it only in wording ("Bundling", "sent"
    rather than "linked"), the result type (`BundleResult`), and having no
    per-mirror progress bar: a bundle group has no destination-scan file count
    to bound one against ahead of time. A `BundleSink` never prunes locally —
    a bundle only *records* what the far side must delete — so no
    `prune-scan-*` status ever arrives here; the shared `record()` handling
    stays for parity with the publish path rather than as a silent gap.
    """

    ID_PREFIX = "bun"
    VERB_ING = "Bundling"
    VERB_PAST = "Bundled"
    COUNT_LABEL = "sent"
    SHOW_MIRROR_BAR = False

    DEFAULT_CSS = _transfer_progress_css("BundleProgressModal", "bun")


class BundleModeModal(ModalScreen["bool | None"]):
    """Delta or full, asked every time a bundle starts.

    Returns `True` for full, `False` for delta, `None` on cancel (Escape or
    the Cancel button) — three real outcomes, not two: declining this choice
    must abort the bundle entirely, not silently fall back to delta the way
    `ConfirmModal`'s plain yes/no would if reused here.
    """

    DEFAULT_CSS = """
    BundleModeModal { align: center middle; }
    BundleModeModal > Vertical {
        width: 64;
        max-width: 90%;
        height: auto;
        border: double $primary;
        background: $surface;
        padding: 1 2;
    }
    BundleModeModal .title { text-style: bold; margin-bottom: 1; }
    BundleModeModal Label { width: 100%; margin-bottom: 1; }
    BundleModeModal Horizontal { align: center middle; height: auto; margin-top: 1; }
    BundleModeModal Button { margin: 0 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, network_name: str):
        super().__init__()
        self._network_name = network_name

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Bundle — {_escape(self._network_name)}", classes="title")
            yield Label(
                "Send only what changed since the last bundle, "
                "or the whole network again?"
            )
            with Horizontal():
                yield Button("Cancel", variant="default", id="cancel")
                yield Button("Delta", variant="primary", id="delta")
                yield Button("Full", variant="warning", id="full")

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#delta")
    def _delta(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#full")
    def _full(self) -> None:
        self.dismiss(True)


class PreparePublishModal(ModalScreen[None]):
    """Non-dismissable modal shown during verify and optional dry-run phases."""

    DEFAULT_CSS = """
    PreparePublishModal { align: center middle; }
    PreparePublishModal > Vertical {
        width: 82;
        max-width: 96%;
        height: 26;
        max-height: 90%;
        border: double $primary;
        background: $surface;
    }
    PreparePublishModal #pp-title {
        height: 1;
        background: $primary-darken-3;
        padding: 0 2;
        color: $text-muted;
        text-style: bold;
    }
    PreparePublishModal #pp-phase-area {
        height: auto;
        padding: 1 2 0 2;
    }
    PreparePublishModal #pp-phase { color: $text; text-style: bold; margin-bottom: 1; }
    PreparePublishModal ProgressBar { margin-bottom: 0; }
    PreparePublishModal #pp-log {
        height: 1fr;
        border-top: solid $primary-darken-2;
        background: $background;
        margin-top: 1;
    }
    """

    def __init__(self, network_name: str):
        super().__init__()
        self._network_name = network_name

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"  Preparing  {self._network_name}", id="pp-title")
            with Vertical(id="pp-phase-area"):
                yield Label("", id="pp-phase")
                yield ProgressBar(total=1, show_eta=False, id="pp-pbar")
            log = RichLog(id="pp-log", highlight=False, markup=True, wrap=False, max_lines=1000)
            log.can_focus = False
            yield log

    def set_phase(self, label: str, total: int) -> None:
        try:
            self.query_one("#pp-phase", Label).update(f"  {label}")
            pbar = self.query_one("#pp-pbar", ProgressBar)
            pbar.total = total
            pbar.progress = 0
            self.query_one("#pp-log", RichLog).clear()
        except Exception:
            pass

    def mirror_done(self, idx: int, name: str, detail: str, ok: bool) -> None:
        try:
            self.query_one("#pp-pbar", ProgressBar).progress = idx
            icon = "[green]✓[/]" if ok else "[red]✗[/]"
            detail_markup = f"  [dim]{detail}[/]" if detail else ""
            self.query_one("#pp-log", RichLog).write(f"  {icon}  {name}{detail_markup}")
        except Exception:
            pass

    def on_key(self, event) -> None:
        event.stop()

class CertsModal(ModalScreen[bool]):
    """Generate a private key + CSR for a network's hostnames."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    DEFAULT_CSS = """
    CertsModal { align: center middle; }
    CertsModal > Vertical {
        width: 90%;
        max-width: 110;
        height: auto;
        max-height: 85%;
        border: double $primary;
        background: $surface;
        padding: 1 2;
    }
    CertsModal .modal-title  { text-style: bold; color: $primary; margin-bottom: 1; }
    CertsModal .host-entry   { color: $text; margin-left: 2; }
    CertsModal .path-line    { color: $text-muted; margin-top: 1; }
    CertsModal .error-line   { color: $error; margin-top: 1; }
    CertsModal .success-line { color: $success; margin-top: 1; text-style: bold; }
    CertsModal Horizontal    { align: center middle; height: auto; margin-top: 1; }
    CertsModal Button        { margin: 0 1; }
    CertsModal ScrollableContainer { height: auto; max-height: 30; }
    """

    def __init__(self, network_name: str, hostnames: list[str],
                 key_path: "Path | None" = None, cnf_path: "Path | None" = None,
                 csr_path: "Path | None" = None, md_path: "Path | None" = None,
                 error: str = ""):
        super().__init__()
        self._network_name = network_name
        self._hostnames = hostnames
        self._key_path = key_path
        self._cnf_path = cnf_path
        self._csr_path = csr_path
        self._md_path = md_path
        self._error = error

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"  TLS Certificates — {self._network_name}", classes="modal-title")
            with ScrollableContainer():
                if self._error:
                    yield Label(f"  [red]✗ {_escape(self._error)}[/]", classes="error-line")
                elif self._key_path:
                    yield Label("  [green]✓ Files written[/]", classes="success-line")
                    yield Label(f"  [dim]Key:[/]  {self._key_path}", classes="path-line")
                    yield Label(f"  [dim]Cfg:[/]  {self._cnf_path}", classes="path-line")
                    yield Label(f"  [dim]CSR:[/]  {self._csr_path}", classes="path-line")
                    yield Label(f"  [dim]Docs:[/] {self._md_path}", classes="path-line")
                    yield Label("")
                    yield Label(f"  [bold]{len(self._hostnames)} hostnames covered:[/]")
                    for h in self._hostnames:
                        yield Label(f"    {h}", classes="host-entry")
                    yield Label("")
                    yield Label(
                        f"  Send [bold]{self._csr_path.name}[/] to your CA machine and follow [bold]CERTS.md[/].",
                        classes="path-line",
                    )
                else:
                    yield Label(f"  [bold]{len(self._hostnames)} hostnames to include:[/]")
                    for h in self._hostnames:
                        yield Label(f"    {h}", classes="host-entry")
            with Horizontal():
                if self._key_path or self._error:
                    yield Button("Close", variant="primary", id="btn-close")
                else:
                    yield Button("Cancel", variant="default", id="btn-close")
                    yield Button("Generate", variant="primary", id="btn-generate")

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#btn-close")
    def _close(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#btn-generate")
    def _generate(self) -> None:
        self.dismiss(True)
