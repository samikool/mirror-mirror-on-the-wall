"""Sync — the session started from Sources, shown here, running while
the other sections are browsed.

This page is the Sync mode's root and is never popped, so the SyncView it
mounts lives as long as the app does; switching sections only hides it.
"""
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from mmw.core import config
from mmw.tui.sync import SyncView
from mmw.tui.ui.dialog import ConfirmDialog
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button

_IDLE = ("No sync running. Tick sources in Sources and press s — "
         "or press s with nothing ticked to sync everything.")


class SyncSection(Page):
    TITLE = "Sync"
    BINDINGS = [Binding("x", "stop", "Stop")]
    SCROLLS = False

    def __init__(self):
        super().__init__()
        self.view: SyncView | None = None
        self.resolution_errors = False

    def compose_body(self) -> ComposeResult:
        yield Static(_IDLE, id="sync-idle", markup=False)
        yield Static("", id="sync-resolve", markup=False)

    def on_mount(self):
        self.query_one("#sync-resolve").display = False

    def begin_resolution(self):
        self.resolution_errors = False
        self.query_one("#sync-idle").display = False
        self.query_one("#sync-resolve").display = True
        self.resolution_progress("Resolving sources…")
        self.query_one("#btn-sync-stop", Button).disabled = False
        self.refresh_bindings()

    def resolution_progress(self, text):
        self.query_one("#sync-resolve", Static).update(text)

    def end_resolution(self, text):
        self.resolution_errors = "⚠" in text
        self.resolution_progress(text)
        self.query_one("#sync-resolve").display = bool(text)
        self.query_one("#btn-sync-stop", Button).disabled = True
        self.refresh_bindings()

    def compose_actions(self) -> ComposeResult:
        yield Button("Retry missing", id="btn-sync-retry", compact=True)
        yield Button("Stop", id="btn-sync-stop", variant="warning", compact=True,
                     disabled=True)

    async def start(self, cfg: config.Config, mirrors: list[config.MirrorConfig], *,
                    dry_run: bool = False, resolution_gen: int | None = None,
                    retry_failed: bool = False) -> None:
        """Mount a new session. The caller has already refused a second one."""
        if self.view is not None:
            await self.view.remove()          # a finished session's cards
        if resolution_gen is not None and resolution_gen != self.app._resolve_gen:
            return
        self.view = SyncView(cfg, mirrors, dry_run, retry_failed=retry_failed)
        self.query_one("#sync-idle").display = False
        await self.query_one("#page-body").mount(self.view)
        self.query_one("#btn-sync-stop", Button).disabled = False
        self.refresh_bindings()                 # x Stop, while it runs
        what = "Retrying missing files in" if retry_failed else "Dry run of" if dry_run else "Syncing"
        self.set_status(f"{what} {len(mirrors)} mirror{'s' if len(mirrors) != 1 else ''}…")
        self.view.query_one("#cards").focus()

    @on(SyncView.Completed)
    def _completed(self, event: SyncView.Completed) -> None:
        self.query_one("#btn-sync-stop", Button).disabled = True
        self.refresh_bindings()
        stopped = event.view._stop_event.is_set()
        summary = event.view.summary()
        if self.resolution_errors:
            summary += " Sources failed to resolve; sync is incomplete."
        failed = self.resolution_errors or getattr(event.view, "item_failures", 0)
        self.set_status(summary, "error" if failed else "info" if stopped else "ok")

    @on(Button.Pressed, "#btn-sync-stop")
    def _stop_pressed(self) -> None:
        self._ask_stop()

    @on(Button.Pressed, "#btn-sync-retry")
    def _retry_pressed(self) -> None:
        # A new session also works after reopening the app; no failed-run
        # state or marketplace catalog needs to survive in memory.
        selection = [m.name for m in self.view._mirrors] if self.view else ([], [])
        self.start_flow(lambda: self.app.start_sync(selection, retry_failed=True))

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "stop":
            # Offered (and shown in the footer) only while there is
            # something to stop.
            return getattr(self.app, "_resolving", False) or (self.view is not None and self.view.running)
        return super().check_action(action, parameters)

    def action_stop(self) -> None:
        if getattr(self.app, "_resolving", False) or (self.view is not None and self.view.running):
            self._ask_stop()

    def _ask_stop(self) -> None:
        def answer(yes: bool | None) -> None:
            if yes and getattr(self.app, "_resolving", False):
                self.app.cancel_resolution()
                return
            if yes and self.view is not None:
                self.view.stop()
                self.query_one("#btn-sync-stop", Button).disabled = True
                self.refresh_bindings()
                self.set_status("Stopping — downloads in flight finish, then the pool closes.")
        self.app.push_screen(ConfirmDialog(
            "Stop the sync?", "Downloads stop; files already in the pool are kept.",
            "Stop", "Keep syncing"), answer)
