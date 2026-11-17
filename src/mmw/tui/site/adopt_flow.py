"""Adopting from the far-side TUI: a folder of APT trees mmw didn't make
(or a tree apply.py manages), looked at and checked first — what was found,
where it goes, what isn't adopted — then adopted. The same steps as
`mmw site adopt`; the trees themselves are never changed."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.widgets import Input, Static

from mmw.tui.ui.form import Form, TextField, ToggleField
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


@dataclass(frozen=True)
class AdoptChoice:
    path: str
    as_base: str | None
    key: str | None
    unverified: bool


def _run_adopt(root: str, c: AdoptChoice, dry_run: bool, said=None) -> tuple[int, list[str]]:
    """**Blocking.** `mmw site adopt`, its lines kept. `said(text)`: how far
    hashing has got, now and then (never more than a few times a second)."""
    import time

    from mmw.commands.site import adopt
    from mmw.core.format import fmt_size
    lines: list[str] = []

    def hashing(where):
        seen, last = [0, 0], [0.0]

        def tick(_rel: str, size: int) -> None:
            seen[0] += 1
            seen[1] += size
            now = time.monotonic()
            if said is not None and now - last[0] > 0.25:
                last[0] = now
                said(f"{where}: {seen[0]:,} files done ({fmt_size(seen[1])})")
        return tick
    from mmw.site.ssh import parse_location
    far = parse_location(root)[0] == "ssh"         # the folder is on that host: never this one's ~
    code = adopt(Path(c.path) if far else Path(c.path).expanduser(), Path(root), None, c.as_base or None,
                 Path(c.key).expanduser() if c.key else None, c.unverified, dry_run, True,
                 say=lines.append, err=lines.append, hashing=hashing, said=said)
    return code, lines


class AdoptPage(Page):
    """Step 1: what to adopt, and how — Look shows what was found and checked."""

    TITLE = "Adopt existing trees"
    HELP = ("Takes over APT trees mmw didn't make: each is checked against its Release and\n"
            "indexes and linked into this mirror, its newest suite served. A suite this mirror\n"
            "already has joins it; a new one needs where it is served (deb.debian.org/debian).\n"
            "Without a key, tick Adopt unverified — `mmw site check-signatures` checks later.\n"
            "A mirror over ssh adopts there: the folder is on its host, the key file on this one.")

    def __init__(self, root: str):
        super().__init__()
        from mmw.site.ssh import parse_location
        self._root = root
        self._host = parse_location(root)[1]       # a root over ssh: the folder is on its host
        self._looked: AdoptChoice | None = None

    def compose_body(self) -> ComposeResult:
        yield Form(
            TextField(f"Folder on {self._host}" if self._host else "Folder", "", id="adopt-path",
                      placeholder="where the trees are: /srv/www"),
            TextField("Served at", "", id="adopt-as", placeholder="HOST/PATH, for a repository new here"),
            TextField("Key file", "", id="adopt-key", placeholder="upstream's public key (else this mirror's keys)"),
            ToggleField("Adopt unverified", False, id="adopt-unverified",
                        hint="Without a key: adopted, marked 'signature not checked'"))
        yield Static("", id="adopt-report")

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-adopt-cancel", compact=True)
        yield Button("Look", id="btn-adopt-look", compact=True)
        yield Button("Adopt", id="btn-adopt-go", variant="primary", compact=True)

    def choice(self) -> AdoptChoice:
        values = self.query_one(Form).values()
        return AdoptChoice(values["adopt-path"].strip(), values["adopt-as"].strip() or None,
                           values["adopt-key"].strip() or None, bool(values["adopt-unverified"]))

    @on(Input.Changed)
    def _changed(self, _event=None) -> None:
        self._looked = None

    @on(Button.Pressed, "#btn-adopt-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-adopt-look")
    def _look_pressed(self) -> None:
        c = self.choice()
        if not c.path:
            self.set_status("Say which folder.", "error")
            return
        self._looked = None                     # looking again: its progress shows, its answer replaces
        self.query_one("#adopt-report", Static).update("looking and checking…")
        self._look(c)

    @work(thread=True, exclusive=True, group="adopt-look", exit_on_error=False)
    def _look(self, c: AdoptChoice) -> None:
        try:
            code, lines = _run_adopt(self._root, c, dry_run=True, said=self._said)
        except Exception as e:  # noqa: BLE001 — shown on the page
            code, lines = 1, [str(e)]
        self.app.call_from_thread(self._show, c, code, lines)

    def _said(self, text: str) -> None:
        """From the worker: how far looking has got."""
        def show() -> None:
            if self.is_attached and self._looked is None:
                self.query_one("#adopt-report", Static).update(f"looking and checking… {text}")
        self.app.call_from_thread(show)

    def _show(self, c: AdoptChoice, code: int, lines: list[str]) -> None:
        if not self.is_attached:
            return
        self._looked = c if code == 0 else None
        self.query_one("#adopt-report", Static).update(Text("\n".join(lines), style=None if code == 0 else "yellow"))
        self.set_status("" if code == 0 else "Not yet — see above.", "info" if code == 0 else "error")

    @on(Button.Pressed, "#btn-adopt-go")
    def _go(self) -> None:
        c = self.choice()
        if self._looked != c:
            self.set_status("Look first: adopting goes ahead with what Look found.", "error")
            return
        self.dismiss(c)


class AdoptRunPage(Page):
    """Step 2: adopting — hashing and linking take a while on a big tree."""

    TITLE = "Adopting"

    def __init__(self, root: str, choice: AdoptChoice):
        super().__init__()
        self._root, self._choice = root, choice
        self.finished = False
        self.subtitle = choice.path

    def compose_body(self) -> ComposeResult:
        yield Static("adopting — each file is read and linked into the store…", id="adopt-out")

    def compose_actions(self) -> ComposeResult:
        yield Button("Close", id="btn-adopt-close", variant="primary", compact=True, disabled=True)

    def on_mount(self) -> None:
        self._run()

    @work(thread=True, exclusive=True, group="adopt-run", exit_on_error=False)
    def _run(self) -> None:
        try:
            code, lines = _run_adopt(self._root, self._choice, dry_run=False, said=self._said)
        except Exception as e:  # noqa: BLE001 — shown on the page
            code, lines = 1, [str(e)]
        self.app.call_from_thread(self._done, code, lines)

    def _said(self, text: str) -> None:
        """From the worker: how far adopting has got."""
        def show() -> None:
            if self.is_attached and not self.finished:
                self.query_one("#adopt-out", Static).update(f"adopting — {text}")
        self.app.call_from_thread(show)

    def action_back(self) -> None:
        if self.finished:                         # never mid-run: it carries on either way
            self.dismiss(None)

    def _done(self, code: int, lines: list[str]) -> None:
        self.finished = True
        if not self.is_attached:
            return
        self.query_one("#adopt-out", Static).update(Text("\n".join(lines)))
        self.set_status("adopted" if code == 0 else "nothing adopted", "info" if code == 0 else "error")
        self.query_one("#btn-adopt-close", Button).disabled = False
        self.query_one("#btn-adopt-close", Button).focus()

    @on(Button.Pressed, "#btn-adopt-close")
    def _close(self) -> None:
        self.dismiss(None)
