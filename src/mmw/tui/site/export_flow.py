"""Exporting from the far-side TUI: this mirror's history, for another
network — pick the network (those sent to before, or a new one), the
repositories, and what goes; then the bundle is written on a progress page."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.widgets import Input, Label, Select, SelectionList, Static
from textual.widgets.selection_list import Selection

from mmw.core.format import fmt_size
from mmw.tui.site.run_page import RunPage
from mmw.tui.ui.form import ChoiceField, Form, TextField
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button, JumpSelectionList

NEW = "__new__"
MODES = [("Only what it hasn't had from here", "new"), ("Points dated after a day", "since"),
         ("Everything again", "all")]


@dataclass(frozen=True)
class ExportChoice:
    target: str
    out: str
    repos: frozenset[str]
    mode: str
    since: str | None
    bytes: int
    labels: tuple


def _default_out(network: str, target: str) -> str:
    return str(Path.home() / f"{network}-to-{target or 'network'}-{time.strftime('%Y%m%d')}.tar")


class ExportPage(Page):
    """Step 1: which network, which repositories, what goes — with what that
    would send, worked out as you choose."""

    TITLE = "Export to another network"
    HELP = ("Writes a bundle of this mirror's history for another network. By default only the\n"
            "points not sent to that network before go; the other side applies it with\n"
            "`mmw site apply` and keeps its own nginx and settings.")

    def __init__(self, network: str, targets: list[str], repos: list[tuple[str, str]]):
        super().__init__()
        self._network, self._targets, self._repos = network, targets, repos
        self._plan = None
        self._error: str | None = None
        self.subtitle = f"from {network}"

    def compose_body(self) -> ComposeResult:
        fields = []
        if self._targets:
            fields.append(ChoiceField("Network", [(t, t) for t in self._targets] + [("a new network…", NEW)],
                                      self._targets[0], id="export-known",
                                      hint="Networks this mirror has sent to before, the latest first"))
        fields += [TextField("New network" if self._targets else "Network", "", id="export-target",
                             placeholder="its name, as you'll call it next time"),
                   ChoiceField("Send", MODES, "new", id="export-mode"),
                   TextField("After", "", id="export-since", placeholder="a day: 2026-11-03 (with Send: after a day)"),
                   TextField("Write to", "", id="export-out", placeholder="a .tar path")]
        yield Form(*fields)
        yield Label("Repositories")
        yield JumpSelectionList(*[Selection(Text(label), key, True) for key, label in self._repos],
                                id="export-repos", compact=True)
        yield Static("", id="export-would")

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-export-cancel", compact=True)
        yield Button("Export", id="btn-export-go", variant="primary", compact=True)

    def on_mount(self) -> None:
        self._recheck()

    # ── what is chosen ───────────────────────────────────────────────────────

    def target(self) -> str:
        if self._targets and self.query_one("#export-known", Select).value != NEW:
            return str(self.query_one("#export-known", Select).value)
        return self.query_one("#export-target", Input).value.strip()

    def _choice(self) -> tuple[str, str, str | None, frozenset[str], str]:
        mode = str(self.query_one("#export-mode", Select).value)
        since = self.query_one("#export-since", Input).value.strip() or None
        repos = frozenset(self.query_one("#export-repos", SelectionList).selected)
        target = self.target()
        out = self.query_one("#export-out", Input).value.strip() or _default_out(self._network, target)
        return target, mode, since if mode == "since" else None, repos, out

    @on(Input.Changed)
    @on(Select.Changed)
    @on(SelectionList.SelectedChanged)
    def _changed(self, _event=None) -> None:
        # Only what goes is worked out again — never where it's written —
        # and only once typing pauses: each plan reads every root's points.
        key = self._key()
        if key == getattr(self, "_asked", None):
            return
        if getattr(self, "_timer", None) is not None:
            self._timer.stop()
        self._timer = self.set_timer(0.3, self._recheck)

    def _key(self) -> tuple:
        target, mode, since, repos, _out = self._choice()
        return target, mode, since, repos

    def _say(self, error: str) -> None:
        self._plan, self._error = None, error
        self.query_one("#export-would", Static).update(Text(error, style="yellow"))

    def _recheck(self) -> None:
        key = self._asked = self._key()
        target, mode, since, repos = key
        if not target:
            return self._say("name the network it is for")
        if not repos:
            return self._say("tick at least one repository")
        if mode == "since" and not since:
            return self._say("say the day: points dated after it go")
        self._plan = None
        self._would(key)

    @work(thread=True, exclusive=True, group="export-would", exit_on_error=False)
    def _would(self, key: tuple) -> None:
        from mmw.site import export
        target, mode, since, repos = key
        try:
            plan = export.export_bundle(self.app.mset, target, "-", repos=set(repos), mode=mode, since=since,
                                        dry_run=True)
            error = None
        except Exception as e:  # noqa: BLE001 — nothing new, a bad day: said on the page
            plan, error = None, str(e)
        self.app.call_from_thread(self._show, key, plan, error)

    def _show(self, key: tuple, plan, error: str | None) -> None:
        # A slower answer to an older choice never overwrites the current one.
        if not self.is_attached or key != self._key():
            return
        self._plan, self._error = plan, error
        if error:
            return self._say(error)
        lines = []
        if plan.new_target and plan.target not in self._targets:
            lines.append(f"first export to '{plan.target}' — nothing recorded as sent there, so everything goes")
        lines.append(f"would send {plan.points} point(s), {plan.files:,} file(s), {fmt_size(plan.bytes)}")
        lines += [f"  {label}: {', '.join(points)}" for label, points in sorted(plan.per_repo.items())]
        lines += [f"  skipped: {s}" for s in plan.skipped]
        self.query_one("#export-would", Static).update(Text("\n".join(lines)))

    @on(Button.Pressed, "#btn-export-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-export-go")
    def _go(self) -> None:
        if self._plan is None:
            self.set_status(self._error or "still working out what it would send…", "error")
            return
        target, mode, since, repos, out = self._choice()
        self.dismiss(ExportChoice(target, out, repos, mode, since, self._plan.bytes,
                                  tuple((label, label) for label in sorted(self._plan.per_repo))))


class ExportRunPage(RunPage):
    """Step 2: the bundle written, file by file; Stop leaves nothing behind
    and records nothing as sent."""

    TITLE = "Exporting"
    STOP_NOTE = "stopping leaves no bundle, and nothing is recorded as sent"
    UNCHANGED = "nothing was written, and nothing is recorded as sent"
    RATE = "writing"

    def __init__(self, choice: ExportChoice):
        super().__init__(choice.bytes, list(choice.labels))
        self._choice = choice
        self.subtitle = f"→ {choice.target}"

    def job(self, progress, stop):
        from mmw.site.export import export_bundle
        c = self._choice
        return export_bundle(self.app.mset, c.target, c.out, repos=set(c.repos), mode=c.mode, since=c.since,
                             progress=lambda _e, **d: progress("receive", bytes=d["bytes"], name=d["name"]),
                             stop_event=stop)

    def _done(self, result) -> None:
        from textual.markup import escape
        self.query_one("#run-done", Static).update(
            f"[b $success]✓ wrote {escape(result.out)} — {result.points} point(s) for {escape(result.target)}[/]\n"
            f"next time, only newer points are sent to {escape(result.target)}")
        self.set_status("")
        self._finish()
