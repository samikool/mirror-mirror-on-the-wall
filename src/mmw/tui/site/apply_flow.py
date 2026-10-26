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
from mmw.tui.site.run_page import RunPage
from mmw.tui.ui.widgets import Button, JumpSelectionList

RECENT = "recent-bundles.json"           # on this machine, beside its recent roots


def recent_paths() -> list[str]:
    from mmw.site.recents import state_dir
    try:
        got = json.loads(disk_io.ops.read_text(state_dir() / RECENT))
        return [p for p in got if isinstance(p, str)] if isinstance(got, list) else []
    except (OSError, ValueError):
        return []


def remember(path: str) -> None:
    from mmw.site.recents import state_dir
    paths = [path] + [p for p in recent_paths() if p != path]
    try:
        disk_io.ops.mkdir(state_dir(), parents=True, exist_ok=True)
        disk_io.ops.write_text(state_dir() / RECENT, json.dumps(paths[:5]))
    except OSError:
        pass                               # a convenience, never a failure


@dataclass(frozen=True)
class ApplyChoice:
    src: str
    only: frozenset[str]
    total_bytes: int = 0
    labels: tuple = ()          # (repo, label) of every repository the bundle changes
    forward: frozenset[str] = frozenset()   # held repositories ticked: serve their new version
    place: dict | None = None               # new hostname → root location (None: the first root here)
    sizes: tuple = ()                       # (repo, bytes) from the manifest, for each row's progress


def _row_text(r, location: str) -> str:
    """A repository the bundle changes, and the root it lands in — on which
    machine, for one over ssh."""
    from mmw.tui.site.home import where
    start = f"v{r.from_number}" if r.from_number else "full"
    return (f"{r.label}  {start} → v{r.to_number}  ·  {r.files_sent:,} files  "
            f"{fmt_size(r.bytes_sent)}  ·  {where(location)}")


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
        mset = getattr(self.app, "mset", None)          # none in a bare harness: the path as it is
        where_is = {str(x.info.path): x.info.location for x in (mset.roots if mset else [])}
        return _row_text(r, where_is.get(str(r.root), str(r.root)))

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
                                 tuple((r.repo, r.label) for r in self._changed), picked & held,
                                 sizes=tuple((r.repo, r.bytes_sent) for r in self._changed)))


class ApplyRunPage(RunPage):
    """Steps 2 and 3: the bundle arriving, each repository switching, and
    then what was done and what needs a person."""

    TITLE = "Applying"
    STOP_NOTE = "stopping keeps every repository already switched; the rest are stored and held"
    UNCHANGED = "the mirror is unchanged"       # apply checks everything before changing anything

    def __init__(self, choice: ApplyChoice):
        super().__init__(choice.total_bytes, list(choice.labels), dict(choice.sizes))
        self._choice = choice
        self.subtitle = Path(choice.src).name
        self._anyway: set[str] = set()          # empty former roots the person said to apply into

    def job(self, progress, stop):
        from mmw.site.apply import apply_to_set, nginx_steps
        mset = self.app.mset
        place = self._choice.place
        if place is None:
            place = {"*": mset.roots[0].info.location} if mset is not None else {}
        result = apply_to_set(mset, self._choice.src, place=place, apply_anyway=self._anyway,
                              only=set(self._choice.only), forward=set(self._choice.forward),
                              progress=progress, stop_event=stop, say=lambda *a: None)
        return result.merged(), nginx_steps(result)

    def handled(self, e: Exception) -> bool:
        from mmw.site.apply import MaybeUnmounted
        if isinstance(e, MaybeUnmounted):
            self.app.call_from_thread(self._ask_anyway, e)
            return True
        return False

    def _ask_anyway(self, e) -> None:
        """A former root that's empty now may be an unmounted share: asked, never assumed."""
        from mmw.tui.ui.dialog import ConfirmDialog

        def answered(ok) -> None:
            if ok:
                self._anyway |= set(e.locations)
                self._run()
            else:
                self._failed(str(e))
        self.app.push_screen(ConfirmDialog("Apply anyway?", str(e), confirm_label="Apply anyway",
                                           confirm_variant="warning", cancel_variant="default"), answered)

    def job_done(self, outcome) -> None:
        self._done(*outcome)

    def _done(self, result, steps=()) -> None:
        for repo in result.updated:
            self._state[repo] = "updated"
        for repo in result.stored_held:
            self._state[repo] = "stored · held"
        for repo in result.refused:
            self._state[repo] = "refused"
        for repo in result.stopped:
            self._state[repo] = "not switched (stopped) · held"
        for repo, state in self._state.items():
            if state in ("waiting", "received", "receiving"):
                self._state[repo] = "unchanged"
        self.query_one("#run-repos", Static).update(self._repo_lines())
        lines = [f"[b $success]✓ applied {result.created}[/]", *map(escape_markup, result.lines)]
        lines += [f"[b $warning]! {escape_markup(a)}[/]" for a in result.actions]
        for name, verb, cmds in steps:
            lines.append(f"[b $warning]! {verb} nginx in {escape_markup(name)} on the serving host:[/]")
            lines += [f"    {escape_markup(c)}" for c in cmds]
        self.query_one("#run-done", Static).update("\n".join(lines))
        self.set_status("")
        self._finish()


def escape_markup(text: str) -> str:
    from textual.markup import escape
    return escape(text)
