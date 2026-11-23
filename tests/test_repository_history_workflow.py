"""Repository history is the authority; network operations only select it."""
from typer.testing import CliRunner
import pytest

from mmw.cli import app
from mmw.commands import history
from mmw.core import config, points
from mmw.core.pool import Pool
from mmw.core.publisher import publish_as_of
from test_transfer_repository_selection import synced, _point, _state  # noqa: F401


def test_unreadable_index_cannot_create_an_incomplete_repository_point(tmp_path):
    from test_repository_points import apt_repository
    from mmw.core.repository_points import record_repository
    cfg, md, units = apt_repository(tmp_path)
    unit = units[0]
    with Pool(md) as pool:
        path = pool.resolve(unit.name, f'dists/{unit.suite}/{unit.component}/binary-{unit.arch}/Packages')
        path.unlink()  # Reproduce a database row whose backing index file is missing.
        with pytest.raises(ValueError, match='unreadable required indexes'):
            record_repository(pool, unit.source, units, md, '20261101T000000Z')
        assert not pool.repository_points(unit.source)
    said = []
    points.record_points_after_sync(cfg, md, {unit.source}, say=said.append)
    assert any('repository points not recorded' in line and 'unreadable' in line for line in said)
    with Pool(md, read_only=True) as pool:
        assert not pool.repository_points(unit.source)


def test_history_cli_and_tui_read_points_without_attaching_them(synced):  # noqa: F811
    from test_snapshot_tui import _run
    from waiting import shows, until
    cfg, net, md, units = synced
    unit = next(u for u in units if u.arch == 'amd64')
    _point(cfg, md, unit, 2, False)
    before = _state(md, net)
    result = CliRunner().invoke(app, ['--config', str(md.parent), 'history', '-n', net.name])
    assert result.exit_code == 0, result.output
    assert '2026-11-01' in result.output and '2026-11-02' in result.output

    async def check(app, pilot):
        await pilot.press('h')
        await until(pilot, lambda: type(app.screen).__name__ == 'HistoryPage' and app.screen.is_mounted)
        await shows(pilot, lambda: app.screen.query_one('#history-found').render(), '2026-11-01', '2026-11-02')
        assert not app.screen.query('#btn-history-add')
    _run(cfg, md.parent, check)
    assert _state(md, net) == before


def test_network_removal_preserves_repository_history(synced):  # noqa: F811
    cfg, net, md, _ = synced
    before = history.prepare(cfg)
    with Pool(md) as pool:
        pool.forget_network(net.name)
    def own(rows):          # what each network was sent goes with the network
        return [r.__class__(**{**r.__dict__, 'marks': tuple(m for m in r.marks if 'sent:' not in m)})
                for r in rows]
    assert own(history.prepare(cfg.model_copy(update={'networks': []}))) == own(before)


def test_upgrade_retires_snapshot_gc_holds_without_losing_history(synced):  # noqa: F811
    import sqlite3
    from mmw.core.snapshots import take_snapshot
    from mmw.core.repository_points import files_for_settings
    cfg, net, md, _ = synced
    snap = take_snapshot(net, cfg, md, label='old-label')
    with Pool(md) as pool:
        db = pool.db_path
        before = dict(pool.snapshot_files(snap.id))
        pool.record_published('/old/published', net.name, snap.stamp)
    with sqlite3.connect(db) as conn:
        conn.execute('DELETE FROM history_upgrades')
    with Pool(md) as pool:
        assert not pool.snapshots(net.name)[0].complete
        ids = pool.snapshot_point_ids(snap.id)
        assert {p: cs for pid in ids for p, cs in files_for_settings(pool, pid, snap.settings).items()} == before
        assert set(ids) <= pool.published_point_ids()
        assert pool.points_at(net.name, snap.stamp)
    assert history.prepare(cfg, network=net.name)


def test_older_publish_without_point_ids_protects_history_until_republished(synced, tmp_path):  # noqa: F811
    import sqlite3
    from mmw.core.publisher import publish_network
    cfg, net, md, units = synced
    net = net.model_copy(update={'root': tmp_path / 'published'})
    cfg = cfg.model_copy(update={'networks': [net], 'settings': cfg.settings.model_copy(update={'keep_points': 1})})
    assert not publish_network(net, cfg, md).errors
    _point(cfg, md, units[0], 2, False)
    with Pool(md) as pool:
        db = pool.db_path
    with sqlite3.connect(db) as conn:
        conn.execute('DELETE FROM history_upgrades')
        conn.execute('DELETE FROM published_points')
    with Pool(md) as pool:
        assert not points.retention_plan(pool, [net], cfg=cfg)
        assert pool.legacy_publish_notes()
    assert 'no resync needed' in history.render(cfg)
    assert not publish_network(net, cfg, md).errors
    with Pool(md, read_only=True) as pool:
        assert not pool.legacy_publish_notes()
        assert points.retention_plan(pool, [net], cfg=cfg)


def test_a_network_cannot_override_repository_retention(synced):  # noqa: F811
    cfg, net, md, units = synced
    unit = next(u for u in units if u.arch == 'amd64')
    cfg = cfg.model_copy(update={'networks': [net], 'settings': cfg.settings.model_copy(update={'keep_points': 1})})
    for day in (2, 3):
        _point(cfg, md, unit, day, day == 3)
        points.record_network_points(net, cfg, md)
    with Pool(md, read_only=True) as pool:
        gone = points.retention_plan(pool, [net], cfg=cfg)
        dates = {p.date[:8] for p in pool.points_of(gone).values()}
        assert '20261101' in dates and '20261102' in dates and '20261103' not in dates


def test_a_published_repository_point_stays_until_the_root_advances(synced, tmp_path):  # noqa: F811
    from mmw.core.publisher import publish_network
    cfg, net, md, units = synced
    unit = next(u for u in units if u.arch == 'amd64')
    net = net.model_copy(update={'root': tmp_path / 'published'})
    cfg = cfg.model_copy(update={'networks': [net], 'settings': cfg.settings.model_copy(update={'keep_points': 1})})
    assert not publish_network(net, cfg, md).errors
    _point(cfg, md, unit, 2, False)
    with Pool(md, read_only=True) as pool:
        assert not points.retention_plan(pool, [net], cfg=cfg)
    assert not publish_network(net, cfg, md).errors
    with Pool(md, read_only=True) as pool:
        gone = points.retention_plan(pool, [net], cfg=cfg)
        assert {p.date[:8] for p in pool.points_of(gone).values()} == {'20261101'}


def test_publish_protects_the_point_it_copied_if_sync_advances_during_the_walk(synced, tmp_path, monkeypatch):  # noqa: F811
    from mmw.core import publisher
    cfg, net, md, units = synced
    unit = next(u for u in units if u.arch == 'amd64')
    net = net.model_copy(update={'root': tmp_path / 'published'})
    real = publisher.walk_network

    def advance(*args, **kwargs):
        answer = real(*args, **kwargs)
        _point(cfg, md, unit, 2, False)
        return answer
    monkeypatch.setattr(publisher, 'walk_network', advance)
    assert not publisher.publish_network(net, cfg, md).errors
    with Pool(md, read_only=True) as pool:
        assert {p.date[:8] for p in pool.points_of(pool.published_point_ids()).values()} == {'20261101'}


def test_as_of_uses_current_selection_even_after_network_recordings(synced, tmp_path):  # noqa: F811
    cfg, net, md, units = synced
    broad = net.model_copy(update={'select': [config.NetworkSelect(source=units[0].source)]})
    points.record_network_points(broad, cfg, md, stamp='20261101T120000Z')
    narrow = net.model_copy(update={'root': tmp_path / 'published'})
    result = publish_as_of(narrow, cfg, md, '2026-11-01')
    assert not result.errors
    assert (narrow.root / 'apt.test/debian/pool/amd64.deb').exists()
    assert not (narrow.root / 'apt.test/debian/pool/arm64.deb').exists()


def test_history_never_carries_a_repository_past_the_selected_target(synced, tmp_path):  # noqa: F811
    import json
    import tarfile
    from mmw.core.publisher.bundle import bundle_network
    from mmw.core.repository_points import current_repository_points, set_current_repository_points
    cfg, net, md, units = synced
    with Pool(md, read_only=True) as pool:
        original = current_repository_points(pool, units[0].source)
    _point(cfg, md, units[0], 3, False)
    # Model an imported later state: retained, but not the live current state.
    with Pool(md) as pool:
        set_current_repository_points(pool, units[0].source, [(pid, False, None) for pid in original])
    result = bundle_network(net, cfg, md, history=True, out_path=tmp_path / 'range.tar')
    with tarfile.open(result.out_path) as tar:
        header = json.loads(tar.extractfile('.mmw/manifest.jsonl').readline())
    assert {p['date'][:8] for p in header['points'][units[0].source]['points']} == {'20261101'}


def test_network_history_dates_ignore_updates_to_unselected_units(synced):  # noqa: F811
    from mmw.core.downloader import MirrorSummary
    from test_repository_points import change_release
    cfg, net, md, units = synced
    arm = next(u for u in units if u.arch == 'arm64')
    with Pool(md) as pool:
        change_release(pool, arm, b'Suite: stable\nDate: Mon, 02 Nov 2026 00:00:00 UTC\n')
    points.record_points_after_sync(cfg, md, {arm.source}, outcomes=[MirrorSummary(mirror=arm.name)],
                                    attempted=[arm], say=lambda _: None)
    rows = history.prepare(cfg, network=net.name)
    assert len(rows) == 1 and rows[0].date[:8] == '20261101' and rows[0].current


def test_snapshot_commands_and_flags_are_removed(synced):  # noqa: F811
    _, _, md, _ = synced
    for args in (['snapshot', 'take'], ['publish', '--snapshot', 'old'],
                 ['publish', '--take-snapshot'], ['bundle', '--to', 'old']):
        result = CliRunner().invoke(app, ['--config', str(md.parent), *args])
        assert result.exit_code == 2, result.output


def test_publish_picker_lists_points_instead_of_snapshots(synced, tmp_path):  # noqa: F811
    from textual.widgets import Select
    from pickers import confirm_picker
    from test_snapshot_tui import _run, _finished
    from waiting import until
    cfg, net, md, _ = synced
    net = net.model_copy(update={'root': tmp_path / 'published'})
    cfg = cfg.model_copy(update={'networks': [net]})

    async def check(app, pilot):
        await pilot.press('p')
        await until(pilot, lambda: type(app.screen).__name__ == 'PublishPickPage'
                    and app.screen.query('#pick-target') and app.screen.query_one('#pick-target').is_mounted)
        assert not app.screen.query('#publish-take-snapshot')
        app.screen.query_one('#pick-target', Select).value = 'point:20261101T000000Z'
        await confirm_picker(app, pilot, 'PublishPickPage')
        await until(pilot, lambda: _finished(app))
        assert app.active_transfer.outcome == 'ok', app.notes
    _run(cfg, md.parent, check)


def test_history_line_reads_a_date_and_time(synced):  # noqa: F811
    import re
    cfg, net, md, _ = synced
    result = CliRunner().invoke(app, ['--config', str(md.parent), 'history', '-n', net.name])
    assert result.exit_code == 0, result.output
    assert 'stable  2026-11-01 00:00:00 UTC' in result.output, result.output
    assert not re.search(r'\d{8}T\d{6}Z|\b\d{6} UTC', result.output)
