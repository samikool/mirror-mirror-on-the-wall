"""One publish or bundle, as a panel — the Publish section's equivalent of
`SyncView`.

Moved out of the full-screen `_TransferProgressModal` so a transfer runs
inside the app while the other sections are browsed. `record()` and the 60 Hz
tick are the modal's, unchanged: `publish_group` (`core/publisher/links.py`)
fires the identical `progress(rel, status)` calls into a `BundleSink` as into
a `TreeSink`, so publish and bundle share the counters, the lock-protected
buffer and the completion states. The two differ only in wording, the widget
id prefix and whether a per-mirror bar renders (`TransferKind`).

A transfer has two stages in one panel: **preparing** (verify, file counts, a
dry run's estimate — `set_phase` / `mirror_done`) and **transferring**
(`begin`, then `record` from the worker thread). It ends in exactly one of
`ok`, `stopped`, `error` or `note` (nothing transferred, for a stated reason —
a dry run not taken further, incomplete mirrors) and posts `Completed`.
"""
import asyncio
import threading
import time
from dataclasses import dataclass

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.markup import escape
from textual.message import Message
from textual.widgets import DataTable, Label, ProgressBar, Static

from mmw.core.format import (
    fmt_duration as _fmt_duration,
    fmt_size as _fmt_bytes,
    fmt_speed as _fmt_speed,
)
from mmw.tui.ui.form import Heading
from mmw.tui.ui.widgets import ThemedLog


@dataclass(frozen=True)
class TransferKind:
    verb_ing: str
    verb_past: str
    count_label: str
    show_mirror_bar: bool
    prefix: str


# A bundle group has no destination-scan file count to bound a per-mirror bar
# against ahead of time, so it shows a running "N considered" count instead.
PUBLISH = TransferKind("Publishing", "Published", "linked", True, "pub")

# Per-file lines (linked, pruned) written per refresh. A 1M-file publish
# queued one per file — far more than anyone reads, and the log's rendering
# cost; the counters carry the totals. Other lines are never capped.
_LOG_LINES_PER_TICK = 50
_FILE_LINE_PREFIXES = ("  [$mmw-ok]→[/] ", "  [$mmw-error]✂[/] ")


def _cap_file_lines(lines: list[str]) -> list[str]:
    out: list[str] = []
    files = 0
    for line in lines:
        if line.startswith(_FILE_LINE_PREFIXES):
            files += 1
            if files > _LOG_LINES_PER_TICK:
                continue
        out.append(line)
    if files > _LOG_LINES_PER_TICK:
        out.append(f"  [dim]… {files - _LOG_LINES_PER_TICK:,} more[/]")
    return out
BUNDLE = TransferKind("Bundling", "Bundled", "sent", False, "bun")


class DryRunReport(Vertical):
    """A dry run's estimate: per-mirror rows, network totals (each file once),
    dist-upgrade pairs. Read-only."""

    def __init__(self, result):
        super().__init__(id="dry-run-report")
        self._result = result

    def compose(self) -> ComposeResult:
        r = self._result
        fmt = _fmt_bytes
        yield DataTable(show_cursor=False, zebra_stripes=True, id="dry-run-table")
        yield Label(
            f"Total  {r.total_files:,} files  ·  {fmt(r.total_bytes)}"
            f"  →  {fmt(r.skipped_bytes)} already published"
            f"  ·  ≈{fmt(r.linked_bytes)} to add (estimated from index diff)",
            classes="totals",
        )
        yield Label("[dim]Each file counted once — mirrors share files, so the rows add up to "
                    "more. Publishing hardlinks from the pool: on the pool's filesystem it "
                    "takes almost no new space.[/]", classes="totals-note")
        if r.errors:
            yield Label(f"[$mmw-error]{len(r.errors)} error(s) — see rows marked ✗[/]",
                        classes="error-row")
        if r.dist_upgrade_paths or r.dist_upgrade_warnings:
            yield Heading("Dist-upgrade")
            if r.dist_upgrade_paths:
                for from_s, to_s in r.dist_upgrade_paths:
                    yield Label(f"[$mmw-ok]✓[/]  {escape(from_s)} → {escape(to_s)}",
                                classes="du-line")
            else:
                yield Label("[dim]No consecutive LTS pairs found in this network[/]",
                            classes="du-line")
            for w in r.dist_upgrade_warnings:
                yield Label(f"[$mmw-warn]⚠  {escape(w)}[/]", classes="du-line")

    def on_mount(self) -> None:
        r = self._result
        fmt = _fmt_bytes
        table = self.query_one("#dry-run-table", DataTable)
        # "To Add", not "To Add (est.)": the five headers plus a mirror name
        # like ubuntu-archive-noble-updates-universe-amd64 run past the modal
        # at 100 columns, and the last header is the one that gets clipped —
        # to "To Add (es", which reads as a rendering fault. The estimate is
        # already signalled by the ≈ on the totals line below the table.
        table.add_columns("Mirror", "Files", "Total", "Published", "To Add")
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

class TransferView(Vertical):
    class Completed(Message):
        def __init__(self, view: "TransferView"):
            self.view = view
            super().__init__()

    class DecisionNeeded(Message):
        """A dry run's estimate is on screen: publish it, or stop here."""
        def __init__(self, view: "TransferView"):
            self.view = view
            super().__init__()

    def __init__(self, kind: TransferKind, network_name: str):
        super().__init__(id="transfer-view")
        self.kind = kind
        self.incomplete = False
        # Every checked mirror as (ok, name, detail), incomplete first — set
        # when the completeness check refuses.
        self.mirror_summary: list[tuple[bool, str, str]] = []
        self.incomplete_allowed = False
        # Names the moved code (and its tests) already use.
        self.ID_PREFIX = kind.prefix
        self.VERB_ING = kind.verb_ing
        self.VERB_PAST = kind.verb_past
        self.COUNT_LABEL = kind.count_label
        self.SHOW_MIRROR_BAR = kind.show_mirror_bar
        self._network_name = network_name
        self._total = 0
        self._per_mirror_counts: dict[str, int] = {}
        self._start = time.monotonic()
        self._lock = threading.Lock()
        self._linked = 0
        self._skipped = 0
        self._pruned = 0
        self._log_pending: list[str] = []
        self._mirror_linked = 0
        self._mirror_skipped = 0
        self._mirror_name = ""
        self._mirror_index = 0
        self._mirror_total_count = 0
        self._mirror_total_mirrors = 0
        self._phase = ""
        self._prep_label = self._prep_shown = ""     # set_phase's label; the step shown
        self._pending_mirror_change: tuple | None = None
        self._speed_ema: float = 0.0
        self._last_speed_time: float = 0.0
        self._last_speed_linked: int = 0
        # Archive-writing phase (BundleSink.close()): the real I/O of a bundle.
        self._archive_total_files: int = 0
        self._archive_total_bytes: int = 0
        self._archive_files_done: int = 0
        self._archive_bytes_done: int = 0
        self._archive_files_ema: float = 0.0
        self._archive_bps_ema: float = 0.0
        self._archive_last_sample_time: float = 0.0
        self._archive_last_sample_files: int = 0
        self._archive_last_sample_bytes: int = 0
        # One large file's own progress (BundleSink._ProgressFile).
        self._current_large_file: str = ""
        self._current_large_file_done: int = 0
        self._current_large_file_size: int = 0
        self.stop_event = threading.Event()
        self._transferring = False         # False while preparing
        self._transfer_done: bool = False
        self._result = None
        self._error_msg: str = ""
        self._note: str = ""
        # Whether it was stopped, decided when the work itself returned — a
        # Stop pressed afterwards (during certs, or unmounting a finished
        # panel) must not turn a finished run into a stopped one.
        self._stopped_at_end = False
        self._done_displayed: bool = False
        self._decision: "asyncio.Future[bool] | None" = None

    def compose(self) -> ComposeResult:
        p = self.ID_PREFIX
        yield Static(f"  Preparing  {escape(self._network_name)}", id=f"{p}-title-bar",
                     classes="xfer-title-bar")
        with Vertical(id=f"{p}-progress-area", classes="xfer-progress-area"):
            yield Label("  Overall", id=f"{p}-overall-label", classes="xfer-section-label")
            yield ProgressBar(total=None, show_eta=False, id=f"{p}-pbar")
            yield Label("  Starting…", id=f"{p}-mirror-label", classes="xfer-mirror-label")
            archive_rate_label = Label(
                "", id=f"{p}-archive-rate-label", classes="xfer-archive-rate-label")
            archive_rate_label.display = False
            yield archive_rate_label
            archive_file_label = Label(
                "", id=f"{p}-archive-file-label", classes="xfer-archive-file-label")
            archive_file_label.display = False
            yield archive_file_label
            if self.SHOW_MIRROR_BAR:
                yield ProgressBar(total=None, show_eta=False, id=f"{p}-mirror-pbar")
            yield Label("", id=f"{p}-stats", classes="xfer-stats")
            yield Label("", id=f"{p}-eta", classes="xfer-eta")
        log = ThemedLog(id=f"{p}-log", classes="xfer-log", highlight=False, markup=True,
                        wrap=False, max_lines=2000)
        log.can_focus = False
        yield log
        yield Static("", id=f"{p}-footer-label", classes="xfer-footer-label")

    def on_mount(self) -> None:
        self._last_speed_time = time.monotonic()
        self.app.active_transfer = self
        self._ticker = self.set_interval(1 / 60, self._tick)

    def on_unmount(self) -> None:
        if self.running:
            self.stop_event.set()           # its worker must not outlive it
        if self._decision is not None and not self._decision.done():
            self._decision.set_result(False)
        if getattr(self.app, "active_transfer", None) is self:
            self.app.active_transfer = None

    # ── state, for the section and the app ──────────────────────────────────

    @property
    def network_name(self) -> str:
        return self._network_name

    @property
    def running(self) -> bool:
        return not self._transfer_done

    @property
    def outcome(self) -> str:
        """`running` | `ok` | `stopped` | `error` | `note`."""
        if not self._transfer_done:
            return "running"
        if self._error_msg:
            return "error"
        if self._note:
            return "note"
        return "stopped" if self._stopped_at_end else "ok"

    @property
    def result(self):
        return self._result

    def progress(self) -> int | None:
        """Percent done, when there is a total to measure against. Capped:
        the file count is an estimate, and "112%" helps nobody."""
        with self._lock:
            if self._archive_total_bytes:
                done, total = self._archive_bytes_done, self._archive_total_bytes
            elif self._transferring and self._total:
                done, total = self._linked + self._skipped, self._total
            else:
                return None
        return min(100, int(100 * done / total))

    @property
    def error_count(self) -> int:
        """Errors a run that *finished* still reported — never a clean success."""
        return len(getattr(self._result, "errors", None) or [])

    def _counts(self) -> str:
        r = self._result
        if r is None:
            return ""
        if hasattr(r, "sent"):
            text = f"{r.sent:,} sent"
            if getattr(r, "deleted", 0):
                text += f", {r.deleted:,} to delete"
        else:
            text = f"{r.linked:,} linked, {r.skipped:,} already current"
            if getattr(r, "pruned", 0):
                text += f", {r.pruned:,} pruned"
        warnings = [*(getattr(r, "dist_upgrade_warnings", None) or []),
                    *(getattr(r, "warnings", None) or [])]
        if warnings:
            text += f", {len(warnings)} warning(s)"
        if self.incomplete_allowed:
            text += ", incomplete content allowed"
        return text

    def summary(self) -> str:
        """The outcome in one line — the section's status and its toast, so
        the two can never disagree, and a run that finished with errors or
        was stopped never reads as a success."""
        name = self._network_name
        outcome = self.outcome
        if outcome == "error":
            return f"{name}: {self._error_msg}"
        if outcome == "note":
            return f"{name}: {self._note}"
        counts = self._counts()
        if outcome == "stopped":
            return f"{self.VERB_ING} {name} stopped" + (f" — {counts}" if counts else "") + "."
        if self.error_count:
            verb = "publish" if self.kind is PUBLISH else "bundle"
            return (f"{name}: {verb} finished with {self.error_count} error(s)"
                    + (f" — {counts}" if counts else "") + ".")
        out = getattr(self._result, "out_path", None)
        return (f"{self.VERB_PAST} {name}" + (f" — {counts}" if counts else "")
                + (f" → {out}" if out else "") + ".")

    @property
    def severity(self) -> str:
        """For the toast: `information`, `warning` or `error`."""
        outcome = self.outcome
        if outcome == "error":
            return "error"
        if outcome == "ok" and not self.error_count and not self.incomplete_allowed and not (
                getattr(self._result, "dist_upgrade_warnings", None)
                or getattr(self._result, "warnings", None)):
            return "information"
        return "warning"

    # ── preparing ───────────────────────────────────────────────────────────

    def set_phase(self, label: str, total: int) -> None:
        p = self.ID_PREFIX
        self._prep_label, self._prep_shown = label, ""
        self.query_one(f"#{p}-mirror-label", Label).update(f"  {escape(label)}")
        bar = self.query_one(f"#{p}-pbar", ProgressBar)
        bar.total = total or None
        bar.progress = 0

    def mirror_done(self, idx: int, name: str, detail: str, ok: bool) -> None:
        self.query_one(f"#{self.ID_PREFIX}-pbar", ProgressBar).progress = idx
        icon = "[$mmw-ok]✓[/]" if ok else "[$mmw-error]✗[/]"
        detail_markup = f"  [dim]{escape(detail)}[/]" if detail else ""
        self.write(f"  {icon}  {escape(name)}{detail_markup}")

    def write(self, line: str) -> None:
        """A log line (markup), in order with what `record()` buffers."""
        with self._lock:
            self._log_pending.append(line)

    async def ask_publish(self, result) -> bool:
        """Show a dry run's estimate and wait for Publish now / Discard."""
        await self.mount(DryRunReport(result), before=self.query_one(f"#{self.ID_PREFIX}-log"))
        self.query_one(f"#{self.ID_PREFIX}-title-bar", Static).update(
            f"  Dry run  {escape(self._network_name)}")
        self.query_one(f"#{self.ID_PREFIX}-progress-area").display = False
        self._decision = asyncio.get_running_loop().create_future()
        self.post_message(self.DecisionNeeded(self))
        go = await self._decision
        self._decision = None
        if self.is_mounted:
            await self.query_one(DryRunReport).remove()
            self.query_one(f"#{self.ID_PREFIX}-progress-area").display = True
        return go

    def decide(self, publish: bool) -> None:
        if self._decision is not None and not self._decision.done():
            self._decision.set_result(publish)

    @property
    def awaiting_decision(self) -> bool:
        return self._decision is not None and not self._decision.done()

    # ── transferring ────────────────────────────────────────────────────────

    def begin(self, total: int = 0, per_mirror_counts: dict[str, int] | None = None) -> None:
        """Preparing is over: the counters, bars and rate start now."""
        p = self.ID_PREFIX
        self._total = total
        self._per_mirror_counts = per_mirror_counts or {}
        self._start = time.monotonic()
        self._last_speed_time = self._start
        bar = self.query_one(f"#{p}-pbar", ProgressBar)
        bar.total = total or None
        bar.progress = 0
        self.query_one(f"#{p}-mirror-label", Label).update("  Waiting for first mirror…")
        self.query_one(f"#{p}-stats", Label).update("  Starting…")
        self.query_one(f"#{p}-title-bar", Static).update(
            f"  {self.VERB_ING}  {escape(self._network_name)}")
        self._transferring = True

    def stop(self) -> None:
        """Workers check the event per file: the current one finishes first."""
        if self._transfer_done or self.stop_event.is_set():
            return
        self.stop_event.set()
        self.decide(False)
        self.query_one(f"#{self.ID_PREFIX}-title-bar", Static).update(
            f"  Stopping  {escape(self._network_name)}  [dim](finishing current file…)[/]")

    def mark_done(self, result, *, stopped: bool | None = None) -> None:
        """From the transfer thread (or its await) when it completes.
        `stopped`: whether the work was stopped, as of when it returned
        (default: the stop event now)."""
        with self._lock:
            self._stopped_at_end = self.stop_event.is_set() if stopped is None else stopped
            self._transfer_done = True
            self._result = result

    def mark_error(self, msg: str) -> None:
        with self._lock:
            self._transfer_done = True
            self._error_msg = msg

    def finish_note(self, note: str, lines: "list[str] | None" = None) -> None:
        """Ended without transferring anything, for a reason worth saying."""
        with self._lock:
            self._log_pending.extend(lines or [])
            self._transfer_done = True
            self._note = note

    def notify_mirror_start(self, name: str, index: int, total_mirrors: int,
                            count: int | None = None) -> None:
        """Called from the transfer thread when a new mirror/group begins.
        `count`: its file count, from a publisher whose groups are not the
        mirrors the pre-publish counts are keyed by (VS Code's hosts)."""
        file_count = self._per_mirror_counts.get(name, 0) if count is None else count
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
            if status.startswith("db-warm:"):
                message = status.split(":", 1)[1]
                self._phase = message if message.endswith("…") else ""
                self._log_pending.append(f"  [dim]# {escape(message)}[/]")
            elif status.startswith("nginx-image:"):
                from mmw.core.publisher.nginx_container import IMAGE
                phase = status.split(":", 1)[1]
                self._phase = "" if phase == "done" else f"Nginx image · {IMAGE} · {phase}"
                self._log_pending.append(f"  [dim]Nginx image · {IMAGE} · {escape(phase)}[/]")
            elif status.startswith("gallery-image:"):
                from mmw.providers.vscode.image import IMAGE
                phase = status.split(":", 1)[1]
                self._phase = "" if phase == "done" else f"Gallery image · {IMAGE} · {phase}"
                self._log_pending.append(f"  [dim]Gallery image · {IMAGE} · {escape(phase)}[/]")
            elif status.startswith("verify:"):
                # Many mirrors verify at once: the line shows the latest step,
                # and the log keeps only each mirror's ✓/✗ line.
                self._phase = status.split(":", 1)[1]
            elif status.startswith("step:"):
                message = status.split(":", 1)[1]
                self._phase = message
                if message:
                    self._log_pending.append(f"  [dim]# {escape(message)}[/]")
            elif status.startswith("gallery:"):
                message = status.split(":", 1)[1]
                self._phase = "" if message == "done" else f"Gallery · {message}"
                if message != "done":
                    self._log_pending.append(f"  [dim]# gallery: {escape(message)}[/]")
            elif status == "linked":
                self._linked += 1
                self._mirror_linked += 1
                self._log_pending.append(f"  [$mmw-ok]→[/] {short}")
            elif status == "skipped":
                self._skipped += 1
                self._mirror_skipped += 1
            elif status in ("linked-meta", "skipped-meta"):
                pass  # metadata ops don't count toward package totals
            elif status == "pruned":
                self._pruned += 1
                self._log_pending.append(f"  [$mmw-error]✂[/] {short}  [dim](pruned)[/]")
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
                # log silent for however long it takes to write, reading
                # as a stall instead of real, in-progress work.
                size = int(status.split(":", 1)[1])
                self._current_large_file = dst
                self._current_large_file_done = 0
                self._current_large_file_size = size
                self._log_pending.append(
                    f"  [$accent]▶[/] writing {short}  [dim]({_fmt_bytes(size)})[/]"
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
                _, files_done, bytes_done = status.split(":")
                self._archive_files_done = int(files_done)
                self._archive_bytes_done = int(bytes_done)
                # `dst` is whichever file happened to complete the instant
                # this (throttled, see BundleSink._maybe_report_archive_
                # progress) report fired — not every file, the same way the
                # numeric counters above only sample rather than tick per
                # file. Enough to show real motion through the archive
                # without a second unthrottled log path into this widget.
                if dst:
                    self._log_pending.append(f"  [$accent]⇢[/] {short}")
                # This file has finished (that's what `archive-progress` fires
                # on) — if it was the large file currently shown as "in
                # progress", clear it so the status line stops claiming it's
                # still being worked on.
                if dst and dst == self._current_large_file:
                    self._current_large_file = ""
                    self._current_large_file_done = 0
                    self._current_large_file_size = 0
            elif status.startswith("archive-done:"):
                _, files_done, bytes_done = status.split(":")
                self._archive_files_done = int(files_done)
                self._archive_bytes_done = int(bytes_done)
                self._current_large_file = ""
                self._current_large_file_done = 0
                self._current_large_file_size = 0
                self._log_pending.append(
                    f"  [dim]# archive written: {int(files_done):,} files, "
                    f"{_fmt_bytes(int(bytes_done))}[/]"
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
            pending = _cap_file_lines(self._log_pending)
            self._log_pending.clear()
            mirror_change = self._pending_mirror_change
            self._pending_mirror_change = None
            phase = self._phase
            archive_total_files = self._archive_total_files
            archive_total_bytes = self._archive_total_bytes
            archive_files_done = self._archive_files_done
            archive_bytes_done = self._archive_bytes_done
            current_large_file = self._current_large_file
            current_large_file_done = self._current_large_file_done
            current_large_file_size = self._current_large_file_size

        total_done = linked + skipped
        if not self._transferring:
            # Preparing: the log, the footer, and the step a long check is on.
            if self._prep_label and phase != self._prep_shown:
                self._prep_shown = phase
                step = f"  [dim]·  {escape(phase)}[/]" if phase else ""
                try:
                    self.query_one(f"#{p}-mirror-label", Label).update(
                        f"  {escape(self._prep_label)}{step}")
                except Exception:
                    pass
            self._flush(pending, now)
            self._complete_once()
            return

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
        # once `archive-start` has fired (see record()).
        if archive_total_files:
            if self._archive_last_sample_time == 0.0:
                self._archive_last_sample_time = now
                self._archive_last_sample_files = archive_files_done
                self._archive_last_sample_bytes = archive_bytes_done
            adt = now - self._archive_last_sample_time
            if adt >= 0.25:
                inst_files = (archive_files_done - self._archive_last_sample_files) / adt
                inst_bytes = (archive_bytes_done - self._archive_last_sample_bytes) / adt
                self._archive_files_ema = 0.3 * inst_files + 0.7 * self._archive_files_ema
                self._archive_bps_ema = 0.3 * inst_bytes + 0.7 * self._archive_bps_ema
                self._archive_last_sample_time = now
                self._archive_last_sample_files = archive_files_done
                self._archive_last_sample_bytes = archive_bytes_done

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
                bar.progress = archive_bytes_done
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
        # cyan for rates, dim for secondary/contextual numbers) — so data
        # progress, file count and speed read as visually distinct at a glance.
        try:
            if archive_total_files:
                # Percentage is bytes-based, same reasoning
                # as the progress bar above — file count alone is misleading
                # when a handful of multi-GB files dominate the actual work.
                pct = (f" [dim]({100 * archive_bytes_done / archive_total_bytes:.0f}%)[/]"
                       if archive_total_bytes else "")
                # Split across three lines/widgets, not one long one: at
                # realistic scale (multi-TB, six-figure file counts) the
                # combined line ran past 130 columns before a filename was
                # even added — well past any terminal width worth assuming.
                self.query_one(f"#{p}-mirror-label", Label).update(
                    f"  [bold]Writing archive[/]"
                    f"  [dim]·[/]  [$mmw-ok]{_fmt_bytes(archive_bytes_done)} / "
                    f"{_fmt_bytes(archive_total_bytes)}[/]{pct}"
                    f"  [dim]·[/]  [dim]{archive_files_done:,} / {archive_total_files:,} files[/]"
                )
                rate_label = self.query_one(f"#{p}-archive-rate-label", Label)
                rate_label.update(
                    f"  [$mmw-running]{self._archive_files_ema:,.0f} files/s[/]"
                    f"  [dim]·[/]  [$mmw-running]{_fmt_speed(self._archive_bps_ema)}[/]"
                )
                rate_label.display = True
                # One big file's own progress, when there is one in flight —
                # see BundleSink._ProgressFile. Filename only, on its own line.
                file_label = self.query_one(f"#{p}-archive-file-label", Label)
                if current_large_file and current_large_file_size:
                    file_pct = 100 * current_large_file_done / current_large_file_size
                    fname = current_large_file.rsplit("/", 1)[-1]
                    file_label.update(f"  [$mmw-warn]▶ writing {fname}: {file_pct:.0f}%[/]")
                    file_label.display = True
                else:
                    file_label.display = False
            elif self._mirror_name:
                # Not in the archive-writing phase (or never entering it at
                # all — the common case for a publish): the two
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

        pruned_str = f"  [$mmw-error]✂ {pruned:,}[/] pruned" if pruned else ""
        try:
            self.query_one(f"#{p}-stats", Label).update(
                f"  [$mmw-ok]↗ {linked:,}[/] {self.COUNT_LABEL}"
                f"  [$mmw-running]= {skipped:,}[/] already current"
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
        self._flush(pending, now)
        self._complete_once()

    def _flush(self, pending: list[str], now: float) -> None:
        p = self.ID_PREFIX
        if pending:
            log = self.query_one(f"#{p}-log", ThemedLog)
            for entry in pending:
                log.write(entry)
        stopping = "  [b $mmw-error]Stopping…[/]  " if (
            self.stop_event.is_set() and not self._transfer_done) else ""
        self.query_one(f"#{p}-footer-label", Static).update(
            f"{stopping}[dim]{escape(self._network_name)}  ·  "
            f"{_fmt_duration(now - self._start)}[/]")

    def _complete_once(self) -> None:
        """Runs once, on the tick after the transfer signals done."""
        if not self._transfer_done or self._done_displayed:
            return
        self._done_displayed = True
        # Nothing moves any more: a finished panel can stay on screen for
        # hours, and a 60 Hz tick behind it is pure waste.
        self._ticker.stop()
        p = self.ID_PREFIX
        outcome = self.outcome
        name = escape(self._network_name)
        title = {
            "error": f"  [b $mmw-error]✗ Error[/]  {name}",
            "stopped": f"  [b $mmw-warn]⚠ Stopped[/]  {name}",
            "note": f"  [b $mmw-warn]⚠ Not {self.VERB_PAST.lower()}[/]  {name}",
            "ok": (f"  [b $mmw-warn]⚠ {self.VERB_PAST} with {self.error_count} error(s)[/]  "
                   f"{name}" if self.error_count else
                   f"  [b $mmw-ok]✓ {self.VERB_PAST}[/]  {name}"),
        }[outcome]
        self.query_one(f"#{p}-title-bar", Static).update(title)
        # A bar that never learned its total animates on its own, ticker or
        # not: a finished panel must not look like it is still working.
        for bar in self.query(ProgressBar):
            if bar.total is None:
                bar.total = 1
                bar.progress = 1 if outcome == "ok" else 0
        if outcome == "error":
            self.query_one(f"#{p}-log", ThemedLog).write(
                f"  [$mmw-error]✗ {escape(self._error_msg)}[/]")
        self.post_message(self.Completed(self))
