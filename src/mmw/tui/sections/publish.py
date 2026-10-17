"""Publish — the publish, dry run or bundle started from Networks, shown here
and running while the other sections are browsed.

The Sync section's twin: this page is the Publish mode's root and is never
popped, so the TransferView it mounts lives as long as the app does. One
transfer at a time; a finished one's panel stays until the next starts.
"""
from dataclasses import replace

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.markup import escape
from textual.widgets import Static

from mmw.core import config
from mmw.tui.publish_flows import BundleChoice, PublishChoice, PublishFlows
from mmw.tui.transfer import BUNDLE, PUBLISH, TransferView
from mmw.tui.ui.dialog import ConfirmDialog
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button

_IDLE = ("Nothing publishing. Highlight a network in Networks and press p to publish, "
         "r for a dry run, or b to bundle.")

VERBS = {"publish": PUBLISH, "dry_run": PUBLISH, "bundle": BUNDLE}


class PublishSection(Page):
    TITLE = "Publish"
    SCROLLS = False
    BINDINGS = [Binding("x", "stop", "Stop")]

    def __init__(self):
        super().__init__()
        self.view: TransferView | None = None
        self._flows: PublishFlows | None = None
        self._last_request = None

    def compose_body(self) -> ComposeResult:
        yield Static(_IDLE, id="publish-idle", markup=False)

    def compose_actions(self) -> ComposeResult:
        yield Button("Discard", id="btn-transfer-discard", compact=True)
        yield Button("Publish now", id="btn-transfer-go", variant="primary", compact=True)
        yield Button("Stop", id="btn-transfer-stop", variant="warning", compact=True,
                     disabled=True)

    def on_mount(self) -> None:
        self._decision_buttons(False)

    def _decision_buttons(self, show: bool) -> None:
        for bid in ("#btn-transfer-discard", "#btn-transfer-go"):
            self.query_one(bid, Button).display = show

    async def start(self, verb: str, net: config.NetworkConfig, *,
                    choice: "PublishChoice | BundleChoice | None" = None) -> None:
        """Mount a new transfer and run its flow. The caller refused a second."""
        if self.view is not None:
            await self.view.remove()          # the finished one's panel
        self._last_request = (verb, net, choice)
        self.view = TransferView(VERBS[verb], net.name)
        self.query_one("#publish-idle").display = False
        await self.query_one("#page-body").mount(self.view)
        self.query_one("#btn-transfer-stop", Button).disabled = False
        self.refresh_bindings()                 # x Stop, while it runs
        what = {"publish": "Publishing", "dry_run": "Dry run of", "bundle": "Bundling"}[verb]
        if isinstance(choice, PublishChoice) and choice.snapshot:
            what = f"Publishing snapshot {choice.snapshot} of"
        self.set_status(f"{what} {net.name}…")
        if self._flows is None:
            self._flows = PublishFlows(self.app, lambda: self.app.cfg)
        view, flows = self.view, self._flows
        runner = {
            "publish": lambda: flows.publish(net, view, choice),
            "dry_run": lambda: flows.dry_run(net, view),
            "bundle": lambda: flows.bundle(net, view, choice),
        }[verb]
        self.start_flow(runner)

    @on(TransferView.DecisionNeeded)
    def _decision_needed(self, event: TransferView.DecisionNeeded) -> None:
        self._decision_buttons(True)
        self.set_status(f"Dry run of {event.view.network_name} done — publish it?")
        self.query_one("#btn-transfer-go", Button).focus()
        if self.app.screen is not self:
            # It holds every other publish until answered: say so wherever
            # the user is (the sidebar shows "4 Publish ▸ ?" too).
            self.notify(f"Dry run of {event.view.network_name} ready — publish it? "
                        "See the Publish section.", timeout=15)

    def _decide(self, publish: bool) -> None:
        self._decision_buttons(False)
        if self.view is not None:
            self.view.decide(publish)
        if publish and self.view is not None:
            self.set_status(f"Publishing {self.view.network_name}…")

    @on(Button.Pressed, "#btn-transfer-go")
    def _go(self) -> None:
        self._decide(True)

    @on(Button.Pressed, "#btn-transfer-discard")
    def _discard(self) -> None:
        self._decide(False)

    @on(TransferView.Completed)
    def _completed(self, event: TransferView.Completed) -> None:
        if event.view is not self.view:
            return                              # a replaced panel's late message
        view = event.view
        self._decision_buttons(False)
        self.query_one("#btn-transfer-stop", Button).disabled = True
        self.refresh_bindings()
        kind = {"information": "ok", "error": "error"}.get(view.severity, "info")
        self.set_status(view.summary(), kind)
        # A toast too: the user may be in another section, and an error or a
        # refusal must never be discovered only by coming back to look.
        self.notify(view.summary(), severity=view.severity,
                    timeout=10 if view.severity == "information" else 20)
        self.app.transfers_finished += 1
        self.app.transfer_finished_signal.publish(None)
        if view.incomplete:
            self._ask_force(view)

    def _ask_force(self, view: TransferView) -> None:
        """The completeness check refused, having checked every mirror: list
        them all, incomplete first, and let one deliberate Yes run it again
        with incomplete content allowed."""
        rows = view.mirror_summary
        bad = sum(1 for ok, _, _ in rows if not ok)
        details = "\n".join(
            f"[$mmw-ok]✓[/] {escape(name)}: [dim]{escape(detail)}[/]" if ok
            else f"[$mmw-error]✗ {escape(name)}[/]: {escape(detail)}"
            for ok, name, detail in rows)
        verb = "Bundle" if view.kind is BUNDLE else "Publish"
        message = (f"{bad} of {len(rows)} mirrors incomplete. {verb} anyway? "
                   "Missing files will be unavailable to clients.")

        async def answer(yes: bool | None) -> None:
            if yes:
                await self._force(view)
        self.app.push_screen(ConfirmDialog(
            f"{view.network_name}: incomplete mirrors", message,
            f"{verb} anyway", "Cancel", confirm_variant="warning", details=details), answer)

    async def _force(self, view: TransferView) -> None:
        if view is not self.view or not view.incomplete or self._last_request is None:
            return                              # another transfer replaced it
        # Cleared first so a second answer cannot launch two transfers.
        view.incomplete = False
        verb, net, choice = self._last_request
        verb = "bundle" if verb == "bundle" else "publish"
        choice = choice or (BundleChoice() if verb == "bundle" else PublishChoice())
        choice = replace(choice, allow_incomplete=True)
        await self.app.start_transfer(verb, net, choice=choice)

    @on(Button.Pressed, "#btn-transfer-stop")
    def _stop_pressed(self) -> None:
        self._ask_stop()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "stop":
            # Offered (and shown in the footer) only while there is
            # something to stop.
            return self.view is not None and self.view.running
        return super().check_action(action, parameters)

    def action_stop(self) -> None:
        if self.view is not None and self.view.running:
            self._ask_stop()

    def _ask_stop(self) -> None:
        def answer(yes: bool | None) -> None:
            if yes and self.view is not None and self.view.running:
                self.view.stop()
                self._decision_buttons(False)
                self.query_one("#btn-transfer-stop", Button).disabled = True
                self.refresh_bindings()
                self.set_status("Stopping — the current file finishes first.")
        self.app.push_screen(ConfirmDialog(
            "Stop?", "Stop after the current file. What is already published stays.",
            "Stop", "Keep going"), answer)
