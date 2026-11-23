"""`mmw history delete` (TUI: Delete points…): deleting connected-side points by hand."""
from typer.testing import CliRunner

from mmw.cli import app
from mmw.commands import history
from mmw.core.pool import Pool
from test_transfer_repository_selection import synced, _point  # noqa: F401


def _delete(md, *args):
    return CliRunner().invoke(app, ['--config', str(md.parent), 'history', 'delete', *args])


def _held(md, repo):
    with Pool(md, read_only=True) as pool:
        return {p.date[:8] for p in pool.repository_points(repo) if p.held}


def _plan(cfg, md, repo, when, **kw):
    with Pool(md, read_only=True) as pool:
        return history.delete_plan(pool, cfg, repo, when, **kw)


def _amd64(units):
    return next(u for u in units if u.arch == 'amd64')


def test_delete_one_date(synced):  # noqa: F811
    cfg, _, md, units = synced
    unit = _amd64(units)
    _point(cfg, md, unit, 2, False)          # 01 Nov keeps pool/amd64.deb alone
    items = _plan(cfg, md, unit.source, '2026-11-01')
    assert [i.date[:8] for i in items] and {i.date[:8] for i in items} == {'20261101'}
    assert not any(i.protected for i in items) and sum(i.frees for i in items) > 0
    result = _delete(md, unit.source, '2026-11-01', '--yes')
    assert result.exit_code == 0, result.output
    assert 'deleted' in result.output and 'freed' in result.output
    assert '20261101' not in _held(md, unit.source) and '20261102' in _held(md, unit.source)
    listing = CliRunner().invoke(app, ['--config', str(md.parent), 'history'])
    assert listing.exit_code == 0 and '2026-11-01' in listing.output and 'no longer kept' in listing.output


def test_delete_before_a_date(synced):  # noqa: F811
    cfg, _, md, units = synced
    unit = _amd64(units)
    _point(cfg, md, unit, 2, True)
    _point(cfg, md, unit, 3, False)
    items = _plan(cfg, md, unit.source, '2026-11-03', before=True)
    assert {i.date[:8] for i in items} == {'20261101', '20261102'}
    result = _delete(md, unit.source, '2026-11-03', '--before', '--yes')
    assert result.exit_code == 0, result.output
    assert _held(md, unit.source) == {'20261103'}


def test_current_point_needs_yes(synced):  # noqa: F811
    cfg, _, md, units = synced
    unit = _amd64(units)
    _point(cfg, md, unit, 2, False)
    result = _delete(md, unit.source, '2026-11-02')
    assert result.exit_code == 1
    assert 'the latest sync' in result.output and 'pass --yes' in result.output
    assert '20261102' in _held(md, unit.source)
    result = _delete(md, unit.source, '2026-11-02', '--yes')
    assert result.exit_code == 0, result.output
    assert '20261102' not in _held(md, unit.source)


def test_published_point_needs_yes(synced, tmp_path):  # noqa: F811
    cfg, net, md, units = synced
    unit = _amd64(units)
    _point(cfg, md, unit, 2, False)
    old = _plan(cfg, md, unit.source, '2026-11-01')
    with Pool(md) as pool:
        pool.record_published(str(tmp_path / 'served'), net.name, 'live', [i.point for i in old])
    result = _delete(md, unit.source, '2026-11-01')
    assert result.exit_code == 1
    assert f'published at {tmp_path / "served"}' in result.output and 'pass --yes' in result.output
    assert '20261101' in _held(md, unit.source)
    result = _delete(md, unit.source, '2026-11-01', '--yes')
    assert result.exit_code == 0, result.output
    assert '20261101' not in _held(md, unit.source)


def test_dry_listing_matches_tui(synced):  # noqa: F811
    from test_snapshot_tui import _run
    from textual.widgets import Input
    from waiting import until
    cfg, _, md, units = synced
    unit = _amd64(units)
    _point(cfg, md, unit, 2, False)
    result = _delete(md, unit.source, '2026-11-02', '--before', '--dry-run')
    assert result.exit_code == 0, result.output
    assert 'dry run' in result.output and '2026-11-01' in result.output
    cli = result.output.split('(dry run')[0].strip()
    before = _held(md, unit.source)

    async def check(app, pilot):
        from mmw.tui.history import HistoryPage
        app.push_screen(HistoryPage())
        await until(pilot, lambda: type(app.screen).__name__ == 'HistoryPage' and app.screen.is_mounted)
        assert ('delete_points', 'Delete points…', 'D', True) in app.screen.more_entries()
        await pilot.press('D')
        await until(pilot, lambda: type(app.screen).__name__ == 'DeletePointsPage' and app.screen.is_mounted)
        app.screen.query_one('#delete-when', Input).value = '2026-11-02'
        app.screen.query_one('#delete-before').value = True
        await pilot.click('#btn-delete-list')
        await until(pilot, lambda: app.screen._items is not None)
        await until(pilot, lambda: str(app.screen.query_one('#delete-plan').render()).strip() == cli)
    _run(cfg, md.parent, check)
    assert _held(md, unit.source) == before
