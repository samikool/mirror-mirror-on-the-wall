"""Look at retained earlier points, then explicitly include them in a network."""
import asyncio

from textual import on
from textual.app import ComposeResult
from textual.widgets import Input, Static

from mmw.commands import network_history as command
from mmw.tui.ui.form import Form, TextField
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


class NetworkHistoryPage(Page):
    TITLE = 'Include earlier points'
    HELP = ('Bring retained repository history into this network. Blank date means all retained; '
            'a date includes that whole day and later. No downloads are needed. '
            'Current serving stays as it is; the next history bundle carries included points.')

    def __init__(self, network):
        super().__init__()
        self.network = network
        self.subtitle = network
        self.plan = None

    def compose_body(self) -> ComposeResult:
        yield Form(
            TextField('Since', '', id='history-since', hint='Blank means all retained points.'),
            TextField('Repositories', '', id='history-repos',
                      hint='Names or URLs, separated by commas. Blank means all selected repositories.'),
        )
        yield Static('', id='history-found', markup=False)

    def compose_actions(self) -> ComposeResult:
        yield Button('Look', id='btn-history-look', compact=True)
        yield Button('Add', id='btn-history-add', variant='primary', compact=True, disabled=True)

    def _raw(self):
        return (self.query_one('#history-since', Input).value.strip(),
                self.query_one('#history-repos', Input).value.strip())

    @on(Input.Changed)
    def _changed(self):
        self.plan = None
        for button in self.query('#btn-history-add').results(Button):
            button.disabled = True
        for found in self.query('#history-found').results(Static):
            found.update('')

    @on(Button.Pressed, '#btn-history-look')
    def action_look(self):
        self.start_flow(self._look)

    async def _look(self):
        raw = self._raw()
        self.set_status('Looking at retained repository points…')
        self.plan = None
        self.query_one('#btn-history-add', Button).disabled = True
        try:
            plan = await asyncio.to_thread(command.prepare, self.app.cfg, self.network, raw[0] or None,
                                           [p.strip() for p in raw[1].split(',') if p.strip()] or None)
        except (ValueError, LookupError, OSError) as e:
            self.set_status(str(e), 'error')
            return
        if self._raw() != raw:
            self.set_status('Selection changed — Look again.')
            return
        self.plan = plan
        self.query_one('#history-found', Static).update(command.describe(self.app.cfg, plan))
        self.query_one('#btn-history-add', Button).disabled = not bool(plan.points)
        self.set_status('')

    @on(Button.Pressed, '#btn-history-add')
    def action_add(self):
        self.start_flow(self._add)

    async def _add(self):
        if self.plan is None:
            return
        self.query_one('#btn-history-add', Button).disabled = True
        try:
            count = await asyncio.to_thread(command.include, self.app.cfg, self.plan)
        except Exception as e:  # noqa: BLE001 — a busy pool or stale preview stays on the page
            self.set_status(str(e), 'error')
            self.plan = None
            return
        self.plan = None
        self.query_one('#history-found', Static).update(f'Included {count} earlier point(s) in {self.network}.')
        self.set_status('Current serving is unchanged.', 'ok')
