"""History from an older mmw converts all at once, at startup: every network,
every step of the upgrade chain, and no point is left legacy — one only a
network walked is that network's own (scoped) point."""
import json
import sqlite3
import tarfile
from types import SimpleNamespace

import pytest
from debs import make_deb
from typer.testing import CliRunner

from mmw.cli import app
from mmw.core import config, legacy_points, points
from mmw.core.layers import layer_dir
from mmw.core.pool import Pool
from mmw.core.publisher.bundle import bundle_network
from mmw.core.repository_points import coordinates, files_for_settings
from mmw.core.snapshots import take_snapshot
from mmw.core.transfer_ranges import preview_transfer
from test_pypi_publish import _src, _sync
from test_repository_points import apt_repository

MARKERS = set(legacy_points.CHAIN)


def _forget_points(md):
    """The pool as an older mmw left it: snapshots only, nothing converted."""
    with Pool(md, read_only=True) as pool:
        db = pool.db_path
    with sqlite3.connect(db) as conn:
        for table in ('points', 'point_files', 'point_units', 'point_unit_files', 'repository_current',
                      'network_points', 'network_recordings', 'network_settings', 'network_repo_rows',
                      'points_converted', 'recordings_seeded', 'snapshot_points', 'sent_migrated',
                      'network_slices', 'network_slice_dates', 'sent_slices'):
            conn.execute(f'DELETE FROM {table}')
        for table in ('history_upgrades', 'published_points', 'point_scopes'):
            conn.execute(f'DROP TABLE {table}')


def _markers(md):
    with Pool(md, read_only=True) as pool:
        return legacy_points.done(pool)


def _legacy_column(md):
    with Pool(md, read_only=True) as pool:
        return pool._conn.execute('SELECT COUNT(*) FROM points WHERE legacy = 1').fetchone()[0]


@pytest.fixture
def old_pool(tmp_path, up):
    """APT and PyPI on network `a`; the APT repository layered on `l`. Each
    snapshotted, then forgotten back to an older mmw's pool."""
    cfg, md, apt_units = apt_repository(tmp_path)
    apt, pypi = cfg.sources[0], _src()
    _sync(cfg.model_copy(update={'sources': [pypi]}))
    a = config.NetworkConfig(name='a', nginx=False, certs=False, bundle_dir=tmp_path / 'usb-a')
    layered = config.NetworkConfig(name='l', nginx=False, certs=False, bundle_dir=tmp_path / 'usb-l',
                                   select=[config.NetworkSelect(source=apt.identity)],
                                   layers=[config.NetworkLayer(source=apt.url, signing='unsigned')])
    cfg = cfg.model_copy(update={'sources': [apt, pypi], 'networks': [a, layered]})
    make_deb(layer_dir(md, layered.name, apt.name) / 'stable/main', 'local-tools', '1.0')
    for net in (a, layered):
        take_snapshot(net, cfg, md, kind='manual')
    _forget_points(md)
    config.save(cfg, tmp_path)
    return cfg, md, a, layered


def test_startup_converts_every_network_at_once(old_pool, tmp_path):
    cfg, md, a, layered = old_pool
    assert not _markers(md)
    result = CliRunner().invoke(app, ['--config', str(tmp_path), 'history'])
    assert result.exit_code == 0, result.output
    assert result.stderr.count('Converting history from an older mmw') == 1
    assert _markers(md) >= MARKERS
    assert _legacy_column(md) == 0
    with Pool(md, read_only=True) as pool:
        assert pool.sent_migrated(a.name) and pool.sent_migrated(layered.name)
        assert not pool._has_table('published_legacy_holds')
        scoped = {pid: pool.point_scopes(pid) for pid in pool.linked_held_points()}
        tracks = {pid: pool.track_of_point(pid).repo for pid in scoped}
    assert {'a'} in scoped.values() and {'l'} in scoped.values()
    assert any(tracks[pid] == 'pypi' and scopes == {'a'} for pid, scopes in scoped.items())
    again = CliRunner().invoke(app, ['--config', str(tmp_path), 'history'])
    assert again.exit_code == 0 and 'Converting history' not in again.stderr


def test_converted_point_serves_only_its_network(old_pool):
    _cfg, md, a, layered = old_pool
    assert points.prepare_history(md)
    with Pool(md, read_only=True) as pool:
        mine = pool.snapshots(a.name)[0].settings
        theirs = pool.snapshots(layered.name)[0].settings
        pid = next(p for p in pool.linked_held_points() if pool.track_of_point(p).repo == 'pypi')
        assert pool.points_of([pid])[pid].legacy                # scoped, so read as before
        assert pool.point_scopes(pid) == {'a'}
        assert files_for_settings(pool, pid, mine) == pool.point_files(pid) != {}
        assert files_for_settings(pool, pid, theirs) == {}
        assert pid not in {p.id for p in pool.repository_points()}


def _members(path):
    with tarfile.open(path) as tar:
        rows = [json.loads(ln) for ln in tar.extractfile('.mmw/manifest.jsonl').read().decode().splitlines()]
    return sorted(row.get('p', '') for row in rows[1:])


def test_conversion_keeps_ids_and_sends_nothing_again(old_pool, tmp_path, monkeypatch):
    cfg, md, _a, layered = old_pool
    with monkeypatch.context() as m:                 # an mmw from before one-model
        m.setattr(legacy_points, '_one_model', lambda pool: [])
        with Pool(md):
            pass
        assert legacy_points.ONE_MODEL not in _markers(md)
        with Pool(md, read_only=True) as pool:
            ids = {p.id: (p.ident, p.files, p.track) for p in pool.points_of(pool.linked_held_points()).values()}
            until = pool.snapshots(layered.name)[0].stamp
        # As of the snapshot: a bundle that walks nothing (a walk records points).
        before = bundle_network(layered, cfg, md, out_path=tmp_path / 'before.tar', until=until, history=True)
        assert not before.errors
        sent = _sent(md, layered.name)
        assert sent
    assert points.prepare_history(md)
    with Pool(md, read_only=True) as pool:
        after = {p.id: (p.ident, p.files, p.track) for p in pool.points_of(pool.linked_held_points()).values()}
    assert after == ids
    assert _sent(md, layered.name) == sent
    assert not preview_transfer(layered, md, cfg=cfg, until=until).blobs     # all of it was sent already
    again = bundle_network(layered, cfg, md, out_path=tmp_path / 'after.tar', until=until, history=True, full=True)
    assert _members(again.out_path) == _members(before.out_path)


def _sent(md, network):
    with Pool(md, read_only=True) as pool:
        return pool.sent_point_ids(network)


def test_published_legacy_holds_become_exact_points(tmp_path):
    cfg, md, units = apt_repository(tmp_path)
    net = config.NetworkConfig(name='a', nginx=False, certs=False)
    cfg = cfg.model_copy(update={'networks': [net]})
    points.record_points_after_sync(cfg, md, {units[0].source}, say=lambda _: None)
    with Pool(md) as pool:
        stamp = pool.first_recording(net.name)
        held = {pid for ids in pool.points_at(net.name, stamp).values() for pid in ids}
        assert held
        pool.record_published('/old/as-of', net.name, points.AS_OF + stamp)
        db = pool.db_path
    with sqlite3.connect(db) as conn:            # as an older mmw left it
        conn.execute('DELETE FROM published_points')
        conn.execute('CREATE TABLE published_legacy_holds (root TEXT PRIMARY KEY)')
        conn.execute("INSERT INTO published_legacy_holds VALUES ('/old/as-of')")
        conn.execute("DELETE FROM history_upgrades WHERE name = 'one-model'")
    assert points.prepare_history(md)
    with Pool(md, read_only=True) as pool:
        assert held <= pool.published_point_ids()
        assert not pool._has_table('published_legacy_holds')
        assert not set(points.retention_plan(pool, [net], cfg=cfg)) & held


def test_pypi_flows_are_distinct_units(tmp_path, up):
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / 'm'), sources=[_src(requirements=['requests'])])
    units = _sync(cfg)
    every = {u.name: u for u in units}
    pairs = [(every[n], every[n.replace('-all-', '-req-')]) for n in every
             if '-all-' in n and n.replace('-all-', '-req-') in every]
    assert pairs
    for all_unit, req_unit in pairs:
        assert all_unit.dims == req_unit.dims
        assert coordinates(all_unit) != coordinates(req_unit)
    req = every[next(n for n in every if n.endswith('-req-universal'))]
    outcomes = [SimpleNamespace(mirror=u.name, ok=u.name != req.name) for u in units]
    points.record_points_after_sync(cfg, cfg.settings.mirror_dir, set(), say=lambda _: None,
                                    outcomes=outcomes, attempted=units)
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        failed = [json.loads(d) for d in pool.failed_units('pypi')]
    assert failed == [{'flow': 'req'}]


def test_walk_records_no_legacy_points(tmp_path, up):
    net = config.NetworkConfig(name='lab', nginx=False, certs=False)
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / 'm'), sources=[_src()], networks=[net])
    _sync(cfg)
    report = points.record_network_points(net, cfg, cfg.settings.mirror_dir)
    assert report.new
    md = cfg.settings.mirror_dir
    assert _legacy_column(md) == 0
    with Pool(md, read_only=True) as pool:
        ids = [p.id for p in pool.network_points(net.name)]
        assert ids and all(pool.point_scopes(pid) == {'lab'} for pid in ids)
        assert all(p.legacy for p in pool.points_of(ids).values())


def test_tui_converts_on_mount_and_lists_the_points(tmp_path):
    from test_snapshot_tui import _run
    from waiting import until
    cfg, md, _units = apt_repository(tmp_path)
    net = config.NetworkConfig(name='a', nginx=False, certs=False)
    cfg = cfg.model_copy(update={'networks': [net]})
    take_snapshot(net, cfg, md, kind='manual')
    _forget_points(md)

    async def check(tui, pilot):
        await until(pilot, lambda: any('History converted' in n for n in tui.notes))
        assert any('Converting history from an older mmw' in n for n in tui.notes)
        await pilot.press('h')
        await until(pilot, lambda: type(tui.screen).__name__ == 'HistoryPage'
                    and tui.screen.query('#history-table')
                    and tui.screen.query_one('#history-table').row_count > 1)
    _run(cfg, tmp_path, check)
    assert _markers(md) >= MARKERS and _legacy_column(md) == 0
