"""`mmw history label` (TUI: Label…): a named point is never pruned, and its
name travels to the far side, where `mmw site serve REPO NAME` serves it and
`mmw site label` (TUI: Label…) names points too."""
from typer.testing import CliRunner

from mmw.cli import app
from mmw.commands import history
from mmw.commands.bundle import bundle_network
from mmw.core import points
from mmw.core.pool import Pool
from test_transfer_repository_selection import synced, _point  # noqa: F401


def _cli(md, *args):
    return CliRunner().invoke(app, ['--config', str(md.parent), 'history', *args])


def _site(far, *args):
    return CliRunner().invoke(app, ['site', *args, '--root', str(far)])


def _amd64(units):
    return next(u for u in units if u.arch == 'amd64')


def _rows(cfg, repo):
    return {r.date[:8]: r for r in history.prepare(cfg, repos=[repo])}


def _held(md, repo):
    with Pool(md, read_only=True) as pool:
        return {p.date[:8] for p in pool.repository_points(repo) if p.held}


def _far(tmp_path, net):
    from mmw.site import home as H
    return H.init_root(tmp_path / 'far', name='main', set_id='work', network=net.name)


def _apply(home, bundle):
    from mmw.site.apply import apply_bundle
    applied = apply_bundle(home, str(bundle), say=lambda *_: None)
    assert not applied.refused and not applied.incomplete


def _far_labels(home, repo):
    from mmw.site.db import SiteDB
    with SiteDB(home) as db:
        return {p.date[:8]: p.snap_label for t in db.tracks(repo) for p in db.points(t.id, stubs=True)}


def _served_day(home, repo):
    from mmw.site.db import SiteDB
    from test_site_apply_timeline import _served_day as served
    with SiteDB(home) as db:
        return served(db, repo)


def test_label_names_a_point_and_shows_in_history(synced):  # noqa: F811
    cfg, _, md, units = synced
    unit = _amd64(units)
    _point(cfg, md, unit, 2, False)
    result = _cli(md, 'label', unit.source, '2026-11-01', 'golden-q4')
    assert result.exit_code == 0, result.output
    assert 'golden-q4' in result.output and '2026-11-01' in result.output
    rows = _rows(cfg, unit.source)
    assert rows['20261101'].label == 'golden-q4' and 'labelled' in rows['20261101'].marks
    assert rows['20261102'].label is None and 'labelled' not in rows['20261102'].marks
    listing = _cli(md)
    line = next(x for x in listing.output.splitlines() if '2026-11-01' in x)
    assert 'labelled golden-q4' in line
    # The same name on another point of the suite moves there.
    result = _cli(md, 'label', unit.source, '2026-11-02', 'golden-q4')
    assert result.exit_code == 0, result.output
    assert 'moved from 2026-11-01' in result.output
    rows = _rows(cfg, unit.source)
    assert rows['20261102'].label == 'golden-q4' and rows['20261101'].label is None
    result = _cli(md, 'label', unit.source, '2026-11-02', '--clear')
    assert result.exit_code == 0, result.output
    assert all(r.label is None for r in _rows(cfg, unit.source).values())


def test_label_refuses_what_it_cannot_name(synced):  # noqa: F811
    cfg, _, md, units = synced
    unit = _amd64(units)
    assert _cli(md, 'label', unit.source, '2026-11-01').exit_code == 2           # no NAME, no --clear
    bad = _cli(md, 'label', unit.source, '2026-11-01', 'two words')
    assert bad.exit_code == 1 and 'letters, digits' in bad.output
    dated = _cli(md, 'label', unit.source, '2026-11-01', '3nov')
    assert dated.exit_code == 1 and 'reads as a date' in dated.output
    none = _cli(md, 'label', unit.source, '2026-10-01', 'x')
    assert none.exit_code == 1 and 'no kept point' in none.output


def test_label_survives_prune_with_keep_1(synced):  # noqa: F811
    cfg, _, md, units = synced
    unit = _amd64(units)
    _point(cfg, md, unit, 2, False)
    _point(cfg, md, unit, 3, True)
    assert _cli(md, 'label', unit.source, '2026-11-01', 'golden-q4').exit_code == 0
    cfg = cfg.model_copy(update={'settings': cfg.settings.model_copy(update={'keep_points': 1})})
    rows = _rows(cfg, unit.source)
    assert 'drops' not in rows['20261101'].marks and 'drops' in rows['20261102'].marks
    with Pool(md) as pool:
        plan = points.retention_plan(pool, cfg.networks, cfg=cfg)
        assert rows['20261101'].point not in plan and rows['20261102'].point in plan
        points.apply_retention(pool, cfg.networks, cfg=cfg)
    assert _held(md, unit.source) == {'20261101', '20261103'}


def test_delete_refuses_a_labelled_point_without_yes(synced):  # noqa: F811
    cfg, _, md, units = synced
    unit = _amd64(units)
    _point(cfg, md, unit, 2, False)
    assert _cli(md, 'label', unit.source, '2026-11-01', 'golden-q4').exit_code == 0
    result = _cli(md, 'delete', unit.source, '2026-11-01')
    assert result.exit_code == 1
    assert 'labelled golden-q4' in result.output and 'pass --yes' in result.output
    assert '20261101' in _held(md, unit.source)
    result = _cli(md, 'delete', unit.source, '2026-11-01', '--yes')
    assert result.exit_code == 0, result.output
    assert '20261101' not in _held(md, unit.source)


def test_label_travels_and_names_the_point_on_the_far_side(synced, tmp_path):  # noqa: F811
    cfg, net, md, units = synced
    unit = _amd64(units)
    _point(cfg, md, unit, 2, False)
    assert _cli(md, 'label', unit.source, '2026-11-01', 'golden-q4').exit_code == 0
    home = _far(tmp_path, net)
    _apply(home, bundle_network(net, cfg, md, out_path=tmp_path / 'a.tar', full=True, history=True).out_path)
    assert _far_labels(home, unit.source) == {'20261101': 'golden-q4', '20261102': None}
    assert _served_day(home, unit.source) == '20261102'
    served = _site(tmp_path / 'far', 'serve', unit.source, 'golden-q4', '--yes')
    assert served.exit_code == 0, served.output
    assert _served_day(home, unit.source) == '20261101'
    shown = _site(tmp_path / 'far', 'history', unit.source)
    assert 'golden-q4' in shown.output
    # A name given after the point was sent, and one cleared, travel with the next bundle.
    assert _cli(md, 'label', unit.source, '2026-11-02', 'rc1').exit_code == 0
    assert _cli(md, 'label', unit.source, '2026-11-01', '--clear').exit_code == 0
    _point(cfg, md, unit, 3, True)
    _apply(home, bundle_network(net, cfg, md, out_path=tmp_path / 'b.tar', history=True).out_path)
    assert _far_labels(home, unit.source) == {'20261101': None, '20261102': 'rc1', '20261103': None}


def test_far_side_label_protects_and_serves(synced, tmp_path):  # noqa: F811
    from mmw.site import ops
    from mmw.site.home import load_site
    cfg, net, md, units = synced
    unit = _amd64(units)
    _point(cfg, md, unit, 2, False)
    _point(cfg, md, unit, 3, True)
    home = _far(tmp_path, net)
    _apply(home, bundle_network(net, cfg, md, out_path=tmp_path / 'a.tar', full=True, history=True).out_path)
    far = tmp_path / 'far'
    result = _site(far, 'label', unit.source, '2026-11-01', 'golden-q4')
    assert result.exit_code == 0, result.output
    assert 'golden-q4' in result.output
    assert _far_labels(home, unit.source)['20261101'] == 'golden-q4'
    # Clean up keeps it whatever the setting; delete asks again.
    settings = load_site(home).model_copy(update={'keep': 1})
    from mmw.site.db import SiteDB
    with SiteDB(home) as db:
        cleaned = {i.date for i in ops.clean_plan(db, settings)}
    assert '2026-11-01' not in cleaned and '2026-11-02' in cleaned
    import pytest
    with pytest.raises(ValueError, match='labelled golden-q4 — pass --yes'):
        ops.delete_point(home, unit.source, '2026-11-01')
    assert _far_labels(home, unit.source)['20261101'] == 'golden-q4'
    cleared = _site(far, 'label', unit.source, '2026-11-01', '--clear')
    assert cleared.exit_code == 0, cleared.output
    assert 'name golden-q4 cleared' in cleared.output
    assert _far_labels(home, unit.source)['20261101'] is None
    assert _site(far, 'label', unit.source, '2026-11-02', 'rc1').exit_code == 0
    deleted = _site(far, 'delete', unit.source, '2026-11-02', '--yes')
    assert deleted.exit_code == 0, deleted.output
    assert 'labelled rc1' in deleted.output and 'its name goes with it' in deleted.output
    assert _far_labels(home, unit.source)['20261102'] is None


def test_label_in_the_tui(synced):  # noqa: F811
    from test_snapshot_tui import _run
    from textual.widgets import Input
    from waiting import until
    cfg, _, md, units = synced
    unit = _amd64(units)
    _point(cfg, md, unit, 2, False)

    async def check(app, pilot):
        from mmw.tui.history import HistoryPage
        app.push_screen(HistoryPage())
        await until(pilot, lambda: type(app.screen).__name__ == 'HistoryPage' and app.screen.is_mounted
                    and app.screen.selected_row())
        assert ('label_row', 'Label…', 'l', True) in app.screen.more_entries()
        page = app.screen
        t = page.query_one('#history-table')
        t.move_cursor(row=next(i for i, r in enumerate(page._rows) if r and r.date[:8] == '20261101'))
        await pilot.press('l')
        await until(pilot, lambda: type(app.screen).__name__ == 'LabelPage' and app.screen.is_mounted)
        app.screen.query_one('#label-name', Input).value = 'golden-q4'
        await pilot.click('#btn-label-save')
        await until(pilot, lambda: app.screen is page and any(r and r.label == 'golden-q4' for r in page._rows))
        row = next(r for r in page._rows if r and r.label)
        assert row.date[:8] == '20261101'
        await pilot.press('l')
        await until(pilot, lambda: type(app.screen).__name__ == 'LabelPage' and app.screen.is_mounted)
        assert app.screen.query_one('#label-name', Input).value == 'golden-q4'
        await pilot.click('#btn-label-clear')
        await until(pilot, lambda: app.screen is page and not any(r and r.label for r in page._rows))
    _run(cfg, md.parent, check)
    assert all(r.label is None for r in _rows(cfg, unit.source).values())


def test_far_side_label_in_the_tui(synced, tmp_path):  # noqa: F811
    from textual.widgets import Input
    from mmw.site import recents
    from site_points import row_of
    from test_site_tui import _run
    from waiting import until
    cfg, net, md, units = synced
    unit = _amd64(units)
    _point(cfg, md, unit, 2, False)
    home = _far(tmp_path, net)
    _apply(home, bundle_network(net, cfg, md, out_path=tmp_path / 'a.tar', full=True, history=True).out_path)

    async def check(app, pilot):
        from mmw.site.db import SiteDB
        await until(pilot, lambda: type(app.screen).__name__ == 'SiteHomePage' and app.screen.selected_point())
        page = app.screen
        assert ('label', 'Label…', 'l', True) in page.more_entries()
        with SiteDB(home) as db:
            first = next(p for t in db.tracks(unit.source) for p in db.points(t.id) if p.date[:8] == '20261101')
        page.query_one('#site-history').move_cursor(row=row_of(page, first.ident.split(':', 1)[-1][:6]))
        await pilot.press('l')
        await until(pilot, lambda: type(app.screen).__name__ == 'LabelPage' and app.screen.is_mounted)
        app.screen.query_one('#label-name', Input).value = 'golden-q4'
        await pilot.click('#btn-label-save')
        await until(pilot, lambda: _far_labels(home, unit.source).get('20261101') == 'golden-q4')
        await until(pilot, lambda: app.screen is page and page.selected_point()
                    and any(p is not None and p.snap_label == 'golden-q4' for _t, p, *_ in page._history))

    recents.remember(str(tmp_path / 'far'))
    _run(home, check)


def test_remote_label_verb_round_trips(synced, tmp_path):  # noqa: F811
    import io
    import json
    from mmw.site.remote import serve_root_command
    cfg, net, md, units = synced
    unit = _amd64(units)
    home = _far(tmp_path, net)
    _apply(home, bundle_network(net, cfg, md, out_path=tmp_path / 'a.tar', full=True, history=True).out_path)
    out = io.BytesIO()
    args = ['label', str(tmp_path / 'far'), unit.source, '2026-11-01', 'golden-q4', '']
    assert serve_root_command(args, io.BytesIO(), out) == 0
    [line] = json.loads(out.getvalue().decode().splitlines()[-1])['lines']
    assert 'labelled golden-q4' in line
    assert _far_labels(home, unit.source) == {'20261101': 'golden-q4'}
