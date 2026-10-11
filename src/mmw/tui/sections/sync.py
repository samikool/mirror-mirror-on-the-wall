"""Sync — the session started from Repositories, shown here, running while
the other sections are browsed.

This page is the Sync mode's root and is never popped, so the SyncView it
mounts lives as long as the app does; switching sections only hides it.
"""
from textual import on
from textual.app import ComposeResult
from textual.widgets import Static

from mmw.core import config
from mmw.tui.sync import SyncView
from mmw.tui.ui.dialog import ConfirmDialog
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button

_IDLE = ("No sync running. Tick repositories in Repositories and press s — "
         "or press s with nothing ticked to sync everything.")


class SyncSection(Page):
    TITLE = "Sync"
    SCROLLS = False

    def __init__(self):
        super().__init__()
        self.view: SyncView | None = None

    def compose_body(self) -> ComposeResult:
        yield Static(_IDLE, id="sync-idle", markup=False)

    def compose_actions(self) -> ComposeResult:
        yield Button("Stop", id="btn-sync-stop", variant="warning", compact=True,
                     disabled=True)

    async def start(self, cfg: config.Config, mirrors: list[config.MirrorConfig], *,
                    dry_run: bool = False) -> None:
        """Mount a new session. The caller has already refused a second one."""
        if self.view is not None:
            await self.view.remove()          # a finished session's cards
        self.view = SyncView(cfg, mirrors, dry_run)
        self.query_one("#sync-idle").display = False
        await self.query_one("#page-body").mount(self.view)
        self.query_one("#btn-sync-stop", Button).disabled = False
        what = "Dry run of" if dry_run else "Syncing"
        self.set_status(f"{what} {len(mirrors)} mirror{'s' if len(mirrors) != 1 else ''}…")
        self.view.query_one("#cards").focus()

    @on(SyncView.Completed)
    def _completed(self, event: SyncView.Completed) -> None:
        self.query_one("#btn-sync-stop", Button).disabled = True
        stopped = event.view._stop_event.is_set()
        self.set_status(event.view.summary(), "info" if stopped else "ok")

    @on(Button.Pressed, "#btn-sync-stop")
    def _stop_pressed(self) -> None:
        def answer(yes: bool | None) -> None:
            if yes and self.view is not None:
                self.view.stop()
                self.query_one("#btn-sync-stop", Button).disabled = True
                self.set_status("Stopping — downloads in flight finish, then the pool closes.")
        self.app.push_screen(ConfirmDialog(
            "Stop the sync?", "Downloads stop; files already in the pool are kept.",
            "Stop", "Keep syncing"), answer)
