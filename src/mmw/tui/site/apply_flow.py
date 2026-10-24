"""Applying a bundle from the far-side TUI (approved flow): choose it,
review what it does to each repository, then apply and see what needs a
person."""
import json
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.widgets import Label, SelectionList, Static
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
        self.dismiss(ApplyChoice(self._src, frozenset(self.query_one("#review-repos", SelectionList).selected)))
