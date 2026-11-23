"""Repository points, shared with `mmw history`; no network-owned copy."""
import asyncio

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import Static

from mmw.commands import history as command
from mmw.tui.ui.form import ChoiceField, Form, TextField, ToggleField
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


class HistoryPage(Page):
    TITLE = 'Repository history'
    HELP = ('Sync records repository points. Networks select from that history when publishing '
            'or transferring; no snapshot or history import into a network is needed. '
            'Retention is configured in Sources and Settings. Use Transfer to choose From and To. '
            'Delete points… drops chosen points by hand (mmw history delete).')
    BINDINGS = [Binding('d', 'delete_points', 'Delete points…', show=False)]     # under More…
    MORE = ('delete_points',)

    def __init__(self, *, network=None, repos=None):
        super().__init__()
        self.network = network
        self.repos = repos

    def compose_body(self) -> ComposeResult:
        yield Static('Reading repository history…', id='history-found', markup=False)

    def on_mount(self):
        self.start_flow(self._load)

    def on_screen_resume(self):
        if getattr(self, '_deleted', False):
            self._deleted = False
            self.start_flow(self._load)

    async def _load(self):
        try:
            text = await asyncio.to_thread(command.render, self.app.cfg, network=self.network, repos=self.repos)
        except (ValueError, LookupError, OSError) as exc:
            text = str(exc)
        if self.is_mounted:
            self.query_one('#history-found', Static).update(text)

    def action_delete_points(self) -> None:
        def done(deleted):
            if deleted:
                self._deleted = True
        repo = self.repos[0] if self.repos and len(self.repos) == 1 else None
        self.app.push_screen(DeletePointsPage(repo), done)


class DeletePointsPage(Page):
    """`mmw history delete` as a form: list what goes, then delete it.
    Dismisses with True once something was deleted."""
    TITLE = 'Delete points'
    HELP = ('Kept points of one repository, on a date or before it. Their records stay in the history; '
            'their files go unless another kept point or the latest sync uses them. The latest sync, '
            'published points and points a history network has not been sent yet are protected: '
            'deleting them asks again.')

    def __init__(self, repo=None):
        super().__init__()
        self._repo = repo
        self._items = None

    def compose_body(self) -> ComposeResult:
        choices = [(s.name, s.identity) for s in self.app.cfg.sources] or [('No repositories configured', '')]
        first = self._repo if any(v == self._repo for _, v in choices) else (choices[0][1] if choices else None)
        yield Form(
            ChoiceField('Repository', choices, first, id='delete-repo'),
            TextField('Date', '', id='delete-when', hint='2026-11-03, 3nov or a stamp.'),
            TextField('Suite', '', id='delete-suite', hint='Empty: every suite of that repository.'),
            ToggleField('Before', False, id='delete-before',
                        hint='Every kept point dated before then, not on it (--before).'),
            id='delete-form')
        yield Static('Choose a repository and a date, then List.', id='delete-plan', markup=False)

    def compose_actions(self) -> ComposeResult:
        yield Button('Cancel', id='btn-delete-cancel', compact=True)
        yield Button('List', id='btn-delete-list', compact=True)
        yield Button('Delete', id='btn-delete-go', variant='error', compact=True)

    @on(Button.Pressed, '#btn-delete-cancel')
    def _cancel(self) -> None:
        self.dismiss(False)

    def _choice(self):
        values = self.query_one(Form).values()
        when = str(values['delete-when'] or '').strip()
        if not values['delete-repo'] or not when:
            raise ValueError('Choose a repository and a date.')
        return (values['delete-repo'], when, str(values['delete-suite'] or '').strip() or None,
                bool(values['delete-before']))

    async def _plan(self):
        from mmw.core.pool import Pool
        repo, when, suite, before = self._choice()
        cfg = self.app.cfg

        def read():
            with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
                items = command.delete_plan(pool, cfg, repo, when, suite=suite, before=before)
            return items, command.describe_plan(cfg, items)
        return await asyncio.to_thread(read)

    @on(Button.Pressed, '#btn-delete-list')
    def _list(self) -> None:
        self.start_flow(self._show)

    async def _show(self):
        try:
            self._items, text = await self._plan()
        except (ValueError, LookupError, OSError) as exc:
            self._items = None
            self.set_status(str(exc), 'error')
            return
        if self.is_mounted:
            self.query_one('#delete-plan', Static).update(text)

    @on(Button.Pressed, '#btn-delete-go')
    def _go(self) -> None:
        self.start_flow(self._delete)

    async def _delete(self):
        from mmw.core.format import fmt_size
        from mmw.core.pool import PoolBusy
        from mmw.tui.ui.dialog import ConfirmDialog
        await self._show()
        items = self._items
        if not items:
            if items is not None:
                self.set_status('No kept point matches.', 'error')
            return
        protected = [i for i in items if i.protected]
        if protected:
            title = f'Delete {len(items)} point(s), {len(protected)} of them protected?'
            message = ' · '.join(sorted({i.protected for i in protected})) + '. Their records stay.'
            ok = await self.app.push_screen_wait(ConfirmDialog(title, message, confirm_label='Delete anyway',
                                                               confirm_variant='warning'))
        else:
            ok = await self.app.push_screen_wait(ConfirmDialog(
                f'Delete {len(items)} point(s)?',
                'Their files go too, unless something else keeps them. Their records stay.',
                confirm_label='Delete'))
        if not ok:
            return
        self.set_status(f'Deleting {len(items)} point(s)…')
        try:
            freed = await asyncio.to_thread(command.delete, self.app.cfg, items)
        except (PoolBusy, OSError) as exc:
            self.set_status(str(exc), 'error')
            return
        self.app.notify(f'Deleted {len(items)} point(s); freed {fmt_size(freed)}.')
        self.dismiss(True)
