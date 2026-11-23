"""Repository points, shared with `mmw history`; no network-owned copy."""
import asyncio

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Static

from mmw.commands import history as command
from mmw.tui.ui.form import ChoiceField, Form, TextField, ToggleField
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


class HistoryPage(Page):
    """`mmw history` as a table, laid out like the far side's: a heading row
    per suite, then its points newest first, the latest sync marked ▶. The
    row's point is what Publish as of this, Transfer to this and Delete act on."""
    TITLE = 'Repository history'
    SCROLLS = False
    HELP = ('Sync records repository points. Networks select from that history when publishing '
            'or transferring; no snapshot or history import into a network is needed. '
            'Retention is configured in Sources and Settings.\n'
            'Publish as of this and Transfer to this open the network\'s form with the row\'s date '
            'filled in. Changes lists the packages the row\'s point changed since the point before it '
            '(mmw history diff). Delete drops the row\'s point: its record stays, its files go unless '
            'something else keeps them. Delete points… (More…) drops every point on or before a date '
            '(mmw history delete).')
    BINDINGS = [Binding('p', 'publish_row', 'Publish as of this'),
                Binding('t', 'transfer_row', 'Transfer to this'),
                Binding('c', 'changes_row', 'Changes'),
                Binding('d', 'delete_row', 'Delete'),
                Binding('D', 'delete_points', 'Delete points…', show=False)]     # under More…
    MORE = ('delete_points',)

    def __init__(self, *, network=None, repos=None):
        super().__init__()
        self.network = network
        self.repos = repos
        self._rows: list = []           # per table row: a HistoryRow, or None on a heading
        self._carried: dict[str, set[str]] = {}

    def compose_body(self) -> ComposeResult:
        yield Static('Reading repository history…', id='history-found', markup=False)
        yield DataTable(cursor_type='row', zebra_stripes=True, id='history-table')

    def compose_actions(self) -> ComposeResult:
        yield Button('Publish as of this', id='btn-history-publish', compact=True)
        yield Button('Transfer to this', id='btn-history-transfer', compact=True)
        yield Button('Changes', id='btn-history-changes', compact=True)
        yield Button('Delete', id='btn-history-delete', variant='error', compact=True)

    def on_mount(self):
        self.query_one('#history-table', DataTable).add_columns('', 'Date', 'Size', '')
        self.start_flow(self._load)

    def on_screen_resume(self):
        if getattr(self, '_deleted', False):
            self._deleted = False
            self.start_flow(self._load)

    async def _load(self):
        from mmw.core.pool import Pool
        cfg = self.app.cfg

        def read():
            rows = command.prepare(cfg, network=self.network, repos=self.repos)
            with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
                notes = pool.legacy_publish_notes()
            return rows, notes, command.carried_by(cfg)
        try:
            rows, notes, carried = await asyncio.to_thread(read)
        except (ValueError, LookupError, OSError) as exc:
            rows, notes, carried = (), [str(exc)], {}
        if self.is_mounted:
            self._carried = carried
            self._show(rows, notes)

    def _show(self, rows, notes) -> None:
        from mmw.core.format import fmt_size
        from mmw.core.repos import repository_label
        from mmw.site.versionref import short_day
        cfg = self.app.cfg
        repos = {r.repo for r in rows}
        found = (f'{len(rows)} point(s) in {len(repos)} repositor{"y" if len(repos) == 1 else "ies"} · '
                 f'{sum(r.held for r in rows)} kept · {fmt_size(sum(r.size for r in rows if r.held))}'
                 if rows else 'No repository points recorded yet.')
        self.query_one('#history-found', Static).update('\n'.join([found, *notes]))
        t = self.query_one('#history-table', DataTable)
        keep = t.cursor_row if self._rows else None
        t.clear()
        self._rows = []
        days: dict[tuple, int] = {}
        for r in rows:
            days[(r.repo, r.suite, r.date[:8])] = days.get((r.repo, r.suite, r.date[:8]), 0) + 1
        group = None
        for r in rows:
            if (r.repo, r.suite) != group:
                group = (r.repo, r.suite)
                heading = r.suite if len(repos) == 1 else f'{repository_label(r.repo, cfg)} · {r.suite}'
                t.add_row(Text(heading, style='bold'), '', '', '', key=f'h{len(self._rows)}')
                self._rows.append(None)
            date = short_day(r.date) + (f' {r.date[9:11]}:{r.date[11:13]}'
                                        if days[(r.repo, r.suite, r.date[:8])] > 1 and len(r.date) >= 13 else '')
            words = [command.mark_text(m, r, short=True) for m in r.marks]
            if not r.held:
                words.append('no longer kept')
            if r.legacy:
                words.append('legacy history')
            t.add_row('▶' if r.current else ' ', Text(date), fmt_size(r.size) if r.held else '—',
                      ' · '.join(words), key=f'p{len(self._rows)}')
            self._rows.append(r)
        if keep is not None and t.row_count:
            t.move_cursor(row=min(keep, t.row_count - 1))
        elif t.row_count > 1:           # on the first point, not its heading
            t.move_cursor(row=1)

    def selected_row(self):
        """The HistoryRow at the cursor; None on a heading or an empty table."""
        t = self.query_one('#history-table', DataTable)
        if not self._rows or t.row_count == 0:
            return None
        return self._rows[min(t.cursor_row, len(self._rows) - 1)]

    def _picked(self, verb: str):
        row = self.selected_row()
        if row is None:
            self.set_status('Pick a point in the history first.', 'error')
        elif not row.held and verb != 'delete':
            self.set_status('That point is no longer kept — only its record is.', 'error')
            return None
        return row

    def _when(self, row) -> str:
        """The row's date as the forms take it: its day, or its stamp when
        another point of the repository shares that day."""
        from mmw.site.versionref import day
        same = [r for r in self._rows if r is not None and r.repo == row.repo and r.date[:8] == row.date[:8]]
        return day(row.date) if len(same) == 1 else row.date

    async def _network_for(self, row, verb: str):
        from mmw.tui.ui.dialog import MoreMenu
        nets = {n.name: n for n in self.app.cfg.networks}
        if self.network is not None:
            return nets.get(self.network)
        names = sorted(name for name, repos in self._carried.items() if row.repo in repos and name in nets)
        if not names:
            self.set_status('No network carries this repository — add it to one in Networks.', 'error')
            return None
        if len(names) == 1:
            return nets[names[0]]
        picked = await self.app.push_screen_wait(MoreMenu(
            self.TITLE, [(name, name, '', True) for name in names],
            heading=f'{"Publish" if verb == "publish" else "Transfer"} which network?'))
        return nets.get(picked) if picked else None

    def action_publish_row(self) -> None:
        self.start_flow(lambda: self._transfer('publish'))

    def action_transfer_row(self) -> None:
        self.start_flow(lambda: self._transfer('bundle'))

    async def _transfer(self, verb: str) -> None:
        from mmw.tui.sections.networks import start_transfer
        row = self._picked(verb)
        if row is None:
            return
        net = await self._network_for(row, verb)
        if net is not None:
            await start_transfer(self, verb, net, when=self._when(row))

    @on(Button.Pressed, '#btn-history-publish')
    def _publish_pressed(self) -> None:
        self.action_publish_row()

    @on(Button.Pressed, '#btn-history-transfer')
    def _transfer_pressed(self) -> None:
        self.action_transfer_row()

    @on(Button.Pressed, '#btn-history-changes')
    def _changes_pressed(self) -> None:
        self.action_changes_row()

    def action_changes_row(self) -> None:
        """What the row's point changed since the kept point before it in its suite."""
        from mmw.site.versionref import day_time
        from mmw.tui.changes import ChangesPage
        row = self._picked('changes')
        if row is None:
            return
        at = self._rows.index(row)
        before = next((r for r in self._rows[at + 1:] if r is not None and r.repo == row.repo
                       and r.suite == row.suite and r.held and not r.legacy), None)
        if before is None:
            self.set_status(f'The oldest kept point of {row.suite} — nothing before it to compare with.', 'error')
            return
        cfg = self.app.cfg
        self.app.push_screen(ChangesPage(
            lambda: command.diff_plan(cfg, row.repo, before.date, row.date, suite=row.suite),
            f'{row.suite}: {day_time(before.date)} → {day_time(row.date)}'))

    @on(Button.Pressed, '#btn-history-delete')
    def _delete_pressed(self) -> None:
        self.action_delete_row()

    def action_delete_row(self) -> None:
        self.start_flow(self._delete_row)

    async def _delete_row(self) -> None:
        from mmw.core.format import fmt_size
        from mmw.core.pool import Pool, PoolBusy
        from mmw.site.versionref import day_time
        from textual.markup import escape
        from mmw.tui.ui.dialog import ConfirmDialog
        row = self._picked('delete')
        if row is None:
            return
        if not row.held:
            self.set_status('That point is already deleted — only its record is kept.', 'error')
            return
        cfg = self.app.cfg

        def plan():
            with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
                return [i for i in command.delete_plan(pool, cfg, row.repo, row.date, suite=row.suite)
                        if i.point == row.point]
        items = await asyncio.to_thread(plan)
        if not items:
            self.set_status('That point is no longer kept.', 'error')
            return
        item = items[0]
        title = f'Delete {row.suite} {day_time(row.date)}?'
        if item.protected:
            ok = await self.app.push_screen_wait(ConfirmDialog(
                title, f'It is protected: {escape(item.protected)}. Its record stays.',
                confirm_label='Delete anyway', confirm_variant='warning'))
        else:
            ok = await self.app.push_screen_wait(ConfirmDialog(
                title, f'Frees {fmt_size(item.frees)}. Its record stays.', confirm_label='Delete'))
        if not ok:
            return
        try:
            freed = await asyncio.to_thread(command.delete, cfg, items)
        except (PoolBusy, OSError) as exc:
            self.set_status(str(exc), 'error')
            return
        self.app.notify(f'Deleted 1 point; freed {fmt_size(freed)}.')
        await self._load()

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
