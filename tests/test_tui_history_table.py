"""Repository history in the TUI: a table of points with row actions."""
from textual.widgets import DataTable, Input, Select

from mmw.core.pool import Pool
from test_history_rows import lab  # noqa: F401
from test_snapshot_tui import _run
from test_transfer_repository_selection import synced  # noqa: F401
from waiting import until


async def _open(app, pilot, **kw):
    from mmw.tui.history import HistoryPage
    app.push_screen(HistoryPage(**kw))
    await until(pilot, lambda: type(app.screen).__name__ == 'HistoryPage' and app.screen.is_mounted)
    await until(pilot, lambda: app.screen.query_one('#history-table', DataTable).row_count > 1)
    return app.screen.query_one('#history-table', DataTable)


def _cells(t, row):
    return [str(c) for c in t.get_row_at(row)]


async def _to_day(pilot, page, day):
    t = page.query_one('#history-table', DataTable)
    row = next(i for i, r in enumerate(page._rows) if r is not None and r.date[6:8] == day)
    t.move_cursor(row=row)
    await until(pilot, lambda: page.selected_row() is not None and page.selected_row().date[6:8] == day)


def test_history_table_groups_by_suite(lab):  # noqa: F811
    cfg, oak, md, unit, tmp_path = lab

    async def check(app, pilot):
        t = await _open(app, pilot)
        assert t.row_count == 5                           # one heading, four points
        assert _cells(t, 0)[0] == 'stable' and _cells(t, 0)[1:] == ['', '', '']
        assert [str(c.label) for c in t.columns.values()][1] == 'Released'
        note = str(app.screen.query_one('#history-found').render())
        assert 'Last sync ' in note and 'Released is the date upstream published each point' in note
        newest = _cells(t, 1)
        assert newest[0] == '▶' and newest[1].startswith('04 Nov')
        assert 'latest sync' in newest[3] and 'not yet sent to oak' in newest[3]
        assert [_cells(t, i)[0] for i in (2, 3, 4)] == [' ', ' ', ' ']
        assert 'published at pub' in _cells(t, 3)[3]
        assert app.screen.selected_row().current        # the cursor starts on the newest point
        assert {'publish_row', 'transfer_row', 'delete_row'} <= {
            b.binding.action for b in app.screen.active_bindings.values() if b.binding.show}
        assert ('delete_points', 'Delete points…', 'D', True) in app.screen.more_entries()
    _run(cfg, tmp_path, check)


def test_publish_as_of_this_row_opens_publish_with_its_date(lab):  # noqa: F811
    cfg, oak, md, unit, tmp_path = lab

    async def check(app, pilot):
        await _open(app, pilot)
        await _to_day(pilot, app.screen, '02')
        await pilot.press('p')
        await until(pilot, lambda: type(app.screen).__name__ == 'PublishPickPage' and app.screen.is_mounted)
        assert app.screen.query_one('#pick-target', Select).value == 'date'
        assert app.screen.query_one('#pick-when', Input).value == '2026-11-02'
        assert app.screen.query_one('#field-pick-when').display
    _run(cfg, tmp_path, check)


def test_transfer_to_this_row(lab):  # noqa: F811
    cfg, oak, md, unit, tmp_path = lab

    async def check(app, pilot):
        await _open(app, pilot)
        await _to_day(pilot, app.screen, '03')
        await pilot.click('#btn-history-transfer')
        await until(pilot, lambda: type(app.screen).__name__ == 'BundlePickPage' and app.screen.is_mounted)
        assert app.screen.TITLE == 'Transfer oak'
        assert app.screen.query_one('#pick-to', Select).value == 'date'
        assert app.screen.query_one('#pick-until', Input).value == '2026-11-03'
    _run(cfg, tmp_path, check)


def test_delete_row_confirms_then_stubs(lab):  # noqa: F811
    cfg, oak, md, unit, tmp_path = lab

    def held():
        with Pool(md, read_only=True) as pool:
            return {p.date[:8] for p in pool.repository_points(unit.source) if p.held}

    async def check(app, pilot):
        await _open(app, pilot)
        page = app.screen
        await _to_day(pilot, page, '03')
        await pilot.press('d')
        await until(pilot, lambda: type(app.screen).__name__ == 'ConfirmDialog')
        await pilot.press('escape')                       # cancelled: nothing goes
        await until(pilot, lambda: app.screen is page)
        assert '20261103' in held()
        await pilot.press('d')
        await until(pilot, lambda: type(app.screen).__name__ == 'ConfirmDialog')
        await pilot.click('#confirm')
        await until(pilot, lambda: app.screen is page and '20261103' not in held())
        await until(pilot, lambda: any('Deleted 1 point' in n for n in app.notes))
        t = page.query_one('#history-table', DataTable)
        await until(pilot, lambda: any('no longer kept' in _cells(t, i)[3] for i in range(t.row_count)))
    _run(cfg, tmp_path, check)


def test_delete_protected_row_says_why(lab):  # noqa: F811
    cfg, oak, md, unit, tmp_path = lab

    async def check(app, pilot):
        await _open(app, pilot)
        await _to_day(pilot, app.screen, '02')                # published at pub
        await pilot.press('d')
        await until(pilot, lambda: type(app.screen).__name__ == 'ConfirmDialog')
        text = ' '.join(str(w.render()) for w in app.screen.query('Label, Static'))
        assert 'protected' in text and 'published at' in text
        await pilot.press('escape')
    _run(cfg, tmp_path, check)


def test_history_table_heads_a_suite_with_its_alias(lab):  # noqa: F811
    from test_history_rows import _alias_suite
    cfg, oak, md, unit, tmp_path = lab
    _alias_suite(md, 'oldstable')

    async def check(app, pilot):
        t = await _open(app, pilot)
        assert _cells(t, 0)[0] == 'stable (oldstable)'
    _run(cfg, tmp_path, check)
