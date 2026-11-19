"""Repository points, shared with `mmw history`; no network-owned copy."""
import asyncio

from textual.app import ComposeResult
from textual.widgets import Static

from mmw.commands import history as command
from mmw.tui.ui.page import Page


class HistoryPage(Page):
    TITLE = 'Repository history'
    HELP = ('Sync records repository points. Networks select from that history when publishing '
            'or transferring; no snapshot or history import into a network is needed. '
            'Retention is configured in Sources and Settings. Use Transfer to choose From and To.')

    def __init__(self, *, network=None, repos=None):
        super().__init__()
        self.network = network
        self.repos = repos

    def compose_body(self) -> ComposeResult:
        yield Static('Reading repository history…', id='history-found', markup=False)

    def on_mount(self):
        self.start_flow(self._load)

    async def _load(self):
        try:
            text = await asyncio.to_thread(command.render, self.app.cfg, network=self.network, repos=self.repos)
        except (ValueError, LookupError, OSError) as exc:
            text = str(exc)
        if self.is_mounted:
            self.query_one('#history-found', Static).update(text)
