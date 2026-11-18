"""Add a past point: a Debian or Ubuntu repository's state on a past date (or
at a snapshot URL), from its snapshot archive. Look says what was found;
Add point runs it on the Sync page, like a sync."""
import asyncio

from textual import on
from textual.app import ComposeResult
from textual.markup import escape
from textual.widgets import Input, Static

from mmw.tui.ui.form import Form, TextField
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


class PastPointPage(Page):
    TITLE = "Add a past point"
    HELP = ("A day (2026-10-15) means its end; a snapshot.debian.org or snapshot.ubuntu.com URL "
            "names its own time.\nLook reads the archive's Release and compares it with the "
            "repository's own — nothing is downloaded until Add point.\nThe point is dated by its "
            "Release; what each network is on now is untouched.")

    def __init__(self, repo: str | None = None, when: str = "", *, label: str = ""):
        super().__init__()
        self.repo = repo
        self._when = when
        self.subtitle = label or ("any matching repository" if repo is None else repo)
        self.targets: list = []

    def compose_body(self) -> ComposeResult:
        yield Form(TextField("Date or snapshot URL", self._when, id="past-when",
                             placeholder="2026-10-15 or https://snapshot.debian.org/archive/debian/…",
                             hint="A day, a stamp (20261015T013713Z), or a snapshot URL."))
        yield Static("", id="past-found")

    def compose_actions(self) -> ComposeResult:
        yield Button("Look", id="btn-past-look", compact=True)
        yield Button("Add point", id="btn-past-run", variant="primary", compact=True, disabled=True)

    def on_mount(self) -> None:
        if self._when:
            self.action_look()

    def _raw(self) -> str:
        return self.query_one("#past-when", Input).value.strip()

    @on(Input.Changed)
    def _changed(self) -> None:
        self.targets = []
        self.query_one("#btn-past-run", Button).disabled = True
        self.query_one("#past-found", Static).update("")

    @on(Input.Submitted)
    @on(Button.Pressed, "#btn-past-look")
    def action_look(self) -> None:
        self.start_flow(self._look)

    async def _look(self) -> None:
        from mmw.commands.past_points import look
        self.targets = []
        self.query_one("#btn-past-run", Button).disabled = True
        self.query_one("#past-found", Static).update("")
        raw = self._raw()
        if not raw:
            self.set_status("Give a date or a snapshot URL.", "error")
            return
        url = "://" in raw
        self.set_status("Looking in the archive…")
        try:
            targets = await asyncio.to_thread(
                look, self.app.cfg, self.app.cfg.settings.mirror_dir,
                as_of=None if url else raw, from_url=raw if url else None, repo=self.repo)
        except (ValueError, LookupError) as e:
            self.query_one("#past-found", Static).update(f"[$mmw-error]{escape(str(e))}[/]")
            self.set_status("Nothing to add.", "error")
            return
        self.targets = targets
        self.query_one("#past-found", Static).update(
            "\n".join(f"Found: {escape(t.describe())}" for t in targets))
        self.query_one("#btn-past-run", Button).disabled = False
        self.set_status(f"{len(targets)} repositor{'y' if len(targets) == 1 else 'ies'} found — "
                        "Add point fetches and records it.", "ok")

    @on(Button.Pressed, "#btn-past-run")
    def _run(self) -> None:
        if not self.targets:
            return
        targets = self.targets
        self.app.pop_screen()
        self.app.run_worker(self.app.start_past(targets), group="source-resolution")
