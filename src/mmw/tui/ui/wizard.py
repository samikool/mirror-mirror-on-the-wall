"""Wizard — a Page with numbered steps, Back / Next, and one result.

Steps stay mounted and are shown/hidden by the subclass in `enter()`: Back
must find a step exactly as it was left (pasted text, probe results, picks).
Cancel and Esc dismiss None.
"""
from textual import on
from textual.app import ComposeResult
from textual.widgets import Static

from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


class Wizard(Page):
    STEPS: tuple[str, ...] = ()
    FINISH_LABEL = "Done ✓"

    def __init__(self):
        super().__init__()
        self.step = 0
        self._accepting_next = True

    def compose_body(self) -> ComposeResult:
        yield Static("", id="wizard-steps")

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-wizard-cancel", compact=True)
        yield Button("← Back", id="btn-wizard-back", compact=True, disabled=True)
        yield Button("Next →", id="btn-wizard-next", variant="primary", compact=True)

    async def on_mount(self) -> None:
        await self.go_to(0)

    # ── hooks for subclasses ─────────────────────────────────────────────────

    async def enter(self, step: int) -> None:
        """Show `step`."""

    async def leave(self, step: int) -> bool:
        """Validate and prepare before moving on; False stays on `step`."""
        return True

    def result(self):
        """What Next on the last step dismisses with."""
        return None

    async def finish(self) -> None:
        """Next on the last step. A pushed wizard dismisses with its result; a
        wizard that is a section's root (Setup) overrides this."""
        self.dismiss(self.result())

    def cancel(self) -> None:
        """Cancel. A pushed wizard dismisses None; a root overrides this."""
        self.dismiss(None)

    # ── navigation ───────────────────────────────────────────────────────────

    async def go_to(self, step: int) -> None:
        self.step = step
        last = step == len(self.STEPS) - 1
        self.query_one("#btn-wizard-back", Button).disabled = step == 0
        self.query_one("#btn-wizard-next", Button).label = self.FINISH_LABEL if last else "Next →"
        self.query_one("#wizard-steps", Static).update("  →  ".join(
            f"[b $accent]{name}[/]" if i == step else (f"[dim]{name}[/]" if i < step else name)
            for i, name in enumerate(self.STEPS)))
        await self.enter(step)

    @on(Button.Pressed, "#btn-wizard-next")
    async def _next(self, event: Button.Pressed) -> None:
        event.stop()
        # A double-click or key-repeat queues two presses; handled in order,
        # the second would land on the *next* step's button (skipping Confirm
        # and adding straight away). After moving, refuse Next until a
        # callback queued now has run — it sits behind any press already
        # queued, so only those are dropped.
        if not self._accepting_next:
            return
        # Off from the moment this press is handled — not after the move: a
        # move awaits (Confirm mounts), `step` already reads the new step,
        # and a press made then was queued behind the move and dropped as a
        # double-click while the flag still said Next was taken.
        self._accepting_next = False
        try:
            if not await self.leave(self.step):
                return
            if self.step == len(self.STEPS) - 1:
                await self.finish()
                return
            await self.go_to(self.step + 1)
        finally:
            self.call_later(self._accept_next)

    def _accept_next(self) -> None:
        self._accepting_next = True

    @on(Button.Pressed, "#btn-wizard-back")
    async def _back(self, event: Button.Pressed) -> None:
        event.stop()
        if self.step:
            await self.go_to(self.step - 1)

    @on(Button.Pressed, "#btn-wizard-cancel")
    def _cancel(self, event: Button.Pressed) -> None:
        event.stop()
        self.cancel()
