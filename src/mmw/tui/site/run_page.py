"""The far side's progress page: a long change running, with a bar, its rate
and time left, a line per repository, the file in hand, a Stop that leaves
things whole, and what was done once it ends. Applying a bundle and moving
repositories between roots both run on it."""
from __future__ import annotations

import threading
import time

from textual import on, work
from textual.app import ComposeResult
from textual.markup import escape as escape_markup
from textual.widgets import ProgressBar, Static

from mmw.core.format import fmt_size
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


class RunPage(Page):
    """A subclass gives `job(progress, stop)` (run on a thread; its return is
    handed to `_done`), and may answer an exception in `handled`. `progress`
    takes:

    - `("repo", repo=)`: that repository's bytes start;
    - `("receive"|"copy", bytes=, name=)`: bytes done;
    - `("check")`: everything is in, being checked;
    - `("switch", repo=)`: that repository is switching;
    - `("done")`: the job ended.
    """

    SCROLLS = True
    RATE = "reading"                      # what the bytes are: "reading at 40 MB/s"
    STOP_NOTE = ""                        # what Stop keeps, said while it runs
    UNCHANGED = ""                        # said after a failure that changed nothing

    def __init__(self, total_bytes: int, rows: list[tuple[str, str]], sizes: dict[str, int] | None = None):
        super().__init__()
        self._stop = threading.Event()
        self.finished = False
        self._total = total_bytes
        self._labels = dict(rows)
        self._state = {repo: "waiting" for repo, _label in rows}
        self._sizes = dict(sizes or {})
        self._got: dict[str, int] = {}
        self._read = 0
        self._started = time.monotonic()
        self._current: str | None = None        # the repository whose bytes are arriving
        self._pending_bytes = 0
        self._pending_name = ""
        self._last_report = 0.0

    def compose_body(self) -> ComposeResult:
        yield ProgressBar(total=max(self._total, 1), show_eta=False, id="run-bar")
        yield Static("", id="run-rate")
        yield Static(self._repo_lines(), id="run-repos")
        yield Static("", id="run-file", markup=False)
        yield Static("", id="run-done")

    def compose_actions(self) -> ComposeResult:
        yield Button("Stop", id="btn-run-stop", variant="error", compact=True)
        yield Button("Close", id="btn-run-close", variant="primary", compact=True, disabled=True)

    def on_mount(self) -> None:
        if self.STOP_NOTE:
            self.set_status(self.STOP_NOTE)
        self._run()

    # ── what the job does ────────────────────────────────────────────────────

    def job(self, progress, stop: threading.Event):
        raise NotImplementedError

    def handled(self, e: Exception) -> bool:
        """True when the page answered `e` itself (asked something, say)."""
        return False

    def job_done(self, result) -> None:
        """On the app's thread, with what `job` returned."""
        self._done(result)

    def _done(self, result) -> None:
        raise NotImplementedError

    # ── progress ─────────────────────────────────────────────────────────────

    def _repo_lines(self) -> str:
        """One row per repository: ✓ done, ▶ the one arriving or switching
        (bytes, %, rate, time left), ◇ arrived, · waiting."""
        elapsed = max(time.monotonic() - self._started, 0.001)
        rate = self._read / elapsed
        rows = []
        for r, state in self._state.items():
            label = escape_markup(self._labels.get(r, r))
            if state == "receiving":
                got, size = self._got.get(r, 0), self._sizes.get(r, 0)
                pct = f"  {100 * got // size}%" if size else ""
                left = f" · ~{int((size - got) / rate)}s" if rate and size > got else ""
                rows.append(f"  ▶ {label:<34} {fmt_size(got)} / {fmt_size(size)}{pct}"
                            f"  [dim]· {fmt_size(int(rate))}/s{left}[/]")
            elif state == "switching":
                rows.append(f"  ▶ {label:<34} switching")
            elif state == "received":
                rows.append(f"  ◇ {label:<34} {self.received_note()}")
            elif state == "updated":
                rows.append(f"  [$success]✓[/] {label:<34} {self.updated_note()}")
            elif state == "waiting":
                rows.append(f"  [dim]· {label:<34} waiting[/]")
            else:
                rows.append(f"  ◐ {label:<34} {state}")
        return "\n".join(rows)

    def received_note(self) -> str:
        return "received — switches once everything is checked"

    def updated_note(self) -> str:
        return "updated"

    def _progress(self, event: str, **data) -> None:
        """From the worker. Bytes are added up and handed over at most ten
        times a second: one blocking hand-over per file cost about a
        millisecond each — minutes on a big bundle."""
        if event == "copy":
            event = "receive"
        if event == "receive":
            self._pending_bytes += data.get("bytes", 0)
            self._pending_name = data.get("name", self._pending_name)
            now = time.monotonic()
            if now - self._last_report < 0.1:
                return
            self._last_report = now
            data = {"bytes": self._pending_bytes, "name": self._pending_name}
            self._pending_bytes = 0
        elif self._pending_bytes:
            # What arrived so far belongs to the repository before this event.
            self.app.call_from_thread(self._on_progress, "receive",
                                      {"bytes": self._pending_bytes, "name": self._pending_name})
            self._pending_bytes = 0
        self.app.call_from_thread(self._on_progress, event, data)

    def _on_progress(self, event: str, data: dict) -> None:
        if not self.is_attached:
            return
        if event == "repo":
            if self._current is not None and self._state.get(self._current) == "receiving":
                self._state[self._current] = "received"
            self._current = data["repo"]
            if self._current in self._state:
                self._state[self._current] = "receiving"
            self.query_one("#run-repos", Static).update(self._repo_lines())
            return
        if event == "receive":
            self._read += data.get("bytes", 0)
            if self._current is not None:
                self._got[self._current] = self._got.get(self._current, 0) + data.get("bytes", 0)
            if data.get("name"):
                self.query_one("#run-file", Static).update(f"  {data['name']}")
            self.query_one("#run-repos", Static).update(self._repo_lines())
            self.query_one("#run-bar", ProgressBar).update(progress=self._read)
            elapsed = max(time.monotonic() - self._started, 0.001)
            rate = self._read / elapsed
            left = (self._total - self._read) / rate if rate else 0
            self.query_one("#run-rate", Static).update(
                f"{self.RATE} at {fmt_size(int(rate))}/s · about {int(left // 60)} min {int(left % 60)} s left")
        elif event == "check":
            self.query_one("#run-rate", Static).update("checking everything against the manifest…")
        elif event == "switch":
            for repo in list(self._state):
                if self._state[repo] == "switching":
                    self._state[repo] = "updated"
                elif self._state[repo] == "receiving":
                    self._state[repo] = "received"
            self._state[data["repo"]] = "switching"
            self.query_one("#run-repos", Static).update(self._repo_lines())
            self.query_one("#run-file", Static).update("")
        elif event == "done":
            for repo in list(self._state):
                if self._state[repo] == "switching":
                    self._state[repo] = "updated"
            self.query_one("#run-repos", Static).update(self._repo_lines())

    # ── running, and ending ──────────────────────────────────────────────────

    @work(thread=True, exclusive=True, group="site-run", exit_on_error=False)
    def _run(self) -> None:
        try:
            result = self.job(self._progress, self._stop)
        except Exception as e:  # noqa: BLE001 — the lock, a refusal, a stop: said on the page
            if not self.handled(e):
                self.app.call_from_thread(self._failed, str(e))
            return
        self.app.call_from_thread(self.job_done, result)

    def _finish(self) -> None:
        self.finished = True
        self.query_one("#btn-run-stop", Button).disabled = True
        close = self.query_one("#btn-run-close", Button)
        close.disabled = False
        close.focus()

    def _failed(self, message: str) -> None:
        # A stop says what it left itself; any other failure happened before
        # anything changed.
        text = f"[b $error]✗ {escape_markup(message)}[/]"
        if "stopped" not in message and self.UNCHANGED:
            text += f"\n{self.UNCHANGED}"
        self.query_one("#run-done", Static).update(text)
        self.set_status("")
        self._finish()

    @on(Button.Pressed, "#btn-run-stop")
    def _stop_pressed(self) -> None:
        self._stop.set()
        self.set_status("stopping…")

    @on(Button.Pressed, "#btn-run-close")
    def _close(self) -> None:
        self.dismiss(None)

    def action_back(self) -> None:
        if self.finished:
            self.dismiss(None)
