"""Applying a bundle from the far-side TUI (approved flow): choose it,
review what it does to each repository, then apply and see what needs a
person."""
import json
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
import threading
import time

from textual import on, work
from textual.app import ComposeResult
from textual.widgets import Label, ProgressBar, SelectionList, Static
from textual.widgets.selection_list import Selection

from mmw.core import disk_io
from mmw.core.format import fmt_size
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button, JumpSelectionList

RECENT = "recent.json"


def recent_paths(home: Path) -> list[str]:
    try:
        return json.loads(disk_io.ops.read_text(Path(home) / RECENT))
    except (OSError, ValueError):
        return []


def remember(home: Path, path: str) -> None:
    paths = [path] + [p for p in recent_paths(home) if p != path]
    try:
        disk_io.ops.write_text(Path(home) / RECENT, json.dumps(paths[:5]))
    except OSError:
        pass                               # a convenience, never a failure


@dataclass(frozen=True)
class ApplyChoice:
    src: str
    only: frozenset[str]
    total_bytes: int = 0
    labels: tuple = ()          # (repo, label) of every repository the bundle changes
    forward: frozenset[str] = frozenset()   # held repositories ticked: serve their new version


class ApplyReviewPage(Page):
    """Step 1: every repository the bundle brings, ticked unless held."""

    TITLE = "Apply"
    HELP = ("Ticked repositories switch to the version the bundle brings. Unticked ones —\n"
            "and held ones — are stored and keep serving what they serve; forward them later.")

    def __init__(self, plan, src: str):
        super().__init__()
        self._plan, self._src = plan, src
        self.subtitle = f"{Path(src).name} · {plan.network} → {plan.created}"
        self._changed = [r for r in plan.rows if not r.unchanged]

    def _line(self, r) -> str:
        start = f"v{r.from_number}" if r.from_number else "full"
        return (f"{r.label}  {start} → v{r.to_number}  ·  {r.files_sent:,} files  "
                f"{fmt_size(r.bytes_sent)}  ·  {r.root}")

    def compose_body(self) -> ComposeResult:
        yield JumpSelectionList(*[Selection(Text(self._line(r)), r.repo, not r.held) for r in self._changed],
                                id="review-repos", compact=True)
        for r in self._plan.rows:
            if r.unchanged:
                yield Label(f"[dim]  {r.label}  already at v{r.serving_number}[/]")
        picked = {r.repo for r in self._changed if not r.held}
        yield Static(self._notes(picked), id="review-notes")
        yield Static(self._totals(), id="review-totals")

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-review-cancel", compact=True)
        yield Button("Apply", id="btn-review-go", variant="primary", compact=True)

    def on_mount(self) -> None:
        self.query_one("#review-repos").focus()

    @on(SelectionList.SelectedChanged, "#review-repos")
    def _update(self, _event=None) -> None:
        picked = set(self.query_one("#review-repos", SelectionList).selected)
        self.query_one("#review-notes", Static).update(self._notes(picked))

    def _notes(self, picked: set[str]) -> Text:
        notes = []
        for r in self._changed:
            serving = f"v{r.serving_number}" if r.serving_number else "nothing"
            if r.held and r.repo not in picked:
                notes.append(f"◐ {r.label}: held at {serving}. v{r.to_number} is stored either way; "
                             f"it keeps serving {serving}.")
            elif r.repo not in picked:
                notes.append(f"◐ {r.label}: stored and held — keeps serving {serving}.")
        return Text("\n".join(notes), style="yellow")

    def _totals(self) -> str:
        sent = sum(r.bytes_sent for r in self._changed)
        return (f"always applied: keys · nginx setup · {self._plan.network_files} network file(s)\n"
                f"{fmt_size(self._plan.bytes)} to read · {fmt_size(sent)} for the repositories")

    @on(Button.Pressed, "#btn-review-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-review-go")
    def _go(self) -> None:
        picked = frozenset(self.query_one("#review-repos", SelectionList).selected)
        held = {r.repo for r in self._changed if r.held}
        self.dismiss(ApplyChoice(self._src, picked, self._plan.bytes,
                                 tuple((r.repo, r.label) for r in self._changed), picked & held))


class ApplyRunPage(Page):
    """Steps 2 and 3: the bundle arriving, each repository switching, and
    then what was done and what needs a person."""

    TITLE = "Applying"
    SCROLLS = True

    def __init__(self, choice: ApplyChoice):
        super().__init__()
        self._choice = choice
        self.subtitle = Path(choice.src).name
        self._stop = threading.Event()
        self.finished = False
        self._read = 0
        self._started = time.monotonic()
        self._state = {repo: "waiting" for repo, _label in choice.labels}
        self._pending_bytes = 0
        self._last_report = 0.0

    def compose_body(self) -> ComposeResult:
        yield ProgressBar(total=max(self._choice.total_bytes, 1), show_eta=False, id="run-bar")
        yield Static("", id="run-rate")
        yield Static(self._repo_lines(), id="run-repos")
        yield Static("", id="run-done")

    def compose_actions(self) -> ComposeResult:
        yield Button("Stop", id="btn-run-stop", variant="error", compact=True)
        yield Button("Close", id="btn-run-close", variant="primary", compact=True, disabled=True)

    def on_mount(self) -> None:
        self.set_status("stopping keeps every repository already switched; the rest are stored and held")
        self._run()

    def _repo_lines(self) -> str:
        labels = dict(self._choice.labels)
        return "\n".join(f"  {labels[r]:<36} {state}" for r, state in self._state.items())

    def _progress(self, event: str, **data) -> None:
        """From the worker. Arrivals are added up and handed over at most ten
        times a second: one blocking hand-over per member cost about a
        millisecond each — minutes on a big bundle."""
        if event == "receive":
            self._pending_bytes += data.get("bytes", 0)
            now = time.monotonic()
            if now - self._last_report < 0.1:
                return
            self._last_report = now
            data = {"bytes": self._pending_bytes}
            self._pending_bytes = 0
        self.app.call_from_thread(self._on_progress, event, data)

    def _on_progress(self, event: str, data: dict) -> None:
        if not self.is_attached:
            return
        if event == "receive":
            self._read += data.get("bytes", 0)
            self.query_one("#run-bar", ProgressBar).update(progress=self._read)
            elapsed = max(time.monotonic() - self._started, 0.001)
            rate = self._read / elapsed
            left = (self._choice.total_bytes - self._read) / rate if rate else 0
            self.query_one("#run-rate", Static).update(
                f"reading at {fmt_size(int(rate))}/s · about {int(left // 60)} min {int(left % 60)} s left")
        elif event == "check":
            self.query_one("#run-rate", Static).update("checking everything against the manifest…")
        elif event == "switch":
            for repo in list(self._state):
                if self._state[repo] == "switching":
                    self._state[repo] = "updated"
            self._state[data["repo"]] = "switching"
            self.query_one("#run-repos", Static).update(self._repo_lines())

    @work(thread=True, exclusive=True, group="site-apply", exit_on_error=False)
    def _run(self) -> None:
        from mmw.site.apply import ApplyError, apply_bundle
        try:
            result = apply_bundle(self.app.home, self._choice.src, only=set(self._choice.only),
                                  forward=set(self._choice.forward), progress=self._progress,
                                  stop_event=self._stop, say=lambda *a: None)
        except ApplyError as e:
            self.app.call_from_thread(self._failed, str(e))
            return
        except Exception as e:  # noqa: BLE001 — the lock, and the like
            self.app.call_from_thread(self._failed, str(e))
            return
        self.app.call_from_thread(self._done, result)

    def _finish(self) -> None:
        self.finished = True
        self.query_one("#btn-run-stop", Button).disabled = True
        close = self.query_one("#btn-run-close", Button)
        close.disabled = False
        close.focus()

    def _failed(self, message: str) -> None:
        # A stop says what it left itself; any other failure happened before
        # anything changed (apply checks everything first).
        text = f"[b $error]✗ {escape_markup(message)}[/]"
        if "stopped" not in message:
            text += "\nthe site is unchanged"
        self.query_one("#run-done", Static).update(text)
        self.set_status("")
        self._finish()

    def _done(self, result) -> None:
        for repo in result.updated:
            self._state[repo] = "updated"
        for repo in result.stored_held:
            self._state[repo] = "stored · held"
        for repo in result.refused:
            self._state[repo] = "refused"
        for repo in result.stopped:
            self._state[repo] = "not switched (stopped) · held"
        self.query_one("#run-repos", Static).update(self._repo_lines())
        lines = [f"[b $success]✓ applied {result.created}[/]", *map(escape_markup, result.lines)]
        lines += [f"[b $warning]! {escape_markup(a)}[/]" for a in result.actions]
        self.query_one("#run-done", Static).update("\n".join(lines))
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


def escape_markup(text: str) -> str:
    from textual.markup import escape
    return escape(text)
