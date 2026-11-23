"""Read-only points report, shared by connected and far-side frontends."""
import asyncio

from textual.app import ComposeResult
from textual.widgets import Static

from mmw.commands.verify import describe_points
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


class CheckPointsPage(Page):
    TITLE = 'Check points'

    def __init__(self, load):
        super().__init__()
        self._read_points = load

    def compose_body(self) -> ComposeResult:
        yield Static('Checking every kept point…', id='points-result', markup=False)

    def on_mount(self):
        self.start_flow(self._load)

    async def _load(self):
        try:
            found = await asyncio.to_thread(self._read_points)
            text = describe_points(found)
        except (ValueError, LookupError, OSError) as e:
            text = str(e)
        if self.is_mounted:
            self.query_one('#points-result', Static).update(text)


class VerifyPickPage(Page):
    TITLE = 'Verify'

    def compose_body(self) -> ComposeResult:
        yield Static('Choose what to check. Point checks include retained history.')
        yield Button('Verify served files', id='btn-verify-served')
        yield Button('Check points', id='btn-verify-points')

    def on_button_pressed(self, event):
        if event.button.id in ('btn-verify-served', 'btn-verify-points'):
            event.stop()
            self.dismiss('points' if event.button.id == 'btn-verify-points' else 'served')
