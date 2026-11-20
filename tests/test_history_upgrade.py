"""First history/transfer read upgrades retained history without a sync."""
import sqlite3

import pytest
from typer.testing import CliRunner

from mmw.cli import app
from mmw.commands import bundle, history
from mmw.core import config, points
from mmw.core.pool import Pool
from mmw.core.snapshots import take_snapshot
from test_dry_run_totals import two_mirrors  # noqa: F401


@pytest.fixture
def old_history(two_mirrors):  # noqa: F811
    cfg, net, md = two_mirrors
    snap = take_snapshot(net, cfg, md, kind='manual')
    # Snapshot-only retained history, as in an older instance that hasn't
    # recorded a sync since upgrading to points. Leave blobs and refs alone.
    with Pool(md, read_only=True) as pool:
        db = pool.db_path
    with sqlite3.connect(db) as conn:
        for table in ('points', 'point_files', 'point_units', 'point_unit_files', 'repository_current',
                      'network_points', 'network_recordings', 'network_settings', 'network_repo_rows',
                      'points_converted', 'recordings_seeded', 'snapshot_points'):
            conn.execute(f'DELETE FROM {table}')
        for table in ('history_upgrades', 'published_points', 'published_legacy_holds'):
            conn.execute(f'DROP TABLE {table}')
    config.save(cfg, md.parent)
    return cfg, net, md, snap


def test_first_transfer_read_finds_old_retained_points_without_sync(old_history, monkeypatch):
    cfg, net, md, snap = old_history
    monkeypatch.setattr(points, 'record_points_after_sync', lambda *a, **kw: pytest.fail('synced'))
    assert bundle.available_transfer_dates(net, md, cfg=cfg)
    preview = bundle.preview_transfer(net, md, cfg=cfg)
    assert preview.rows and preview.blobs
    with Pool(md, read_only=True) as pool:
        assert pool.snapshots(net.name)[0].stamp == snap.stamp
        assert pool._conn.execute("SELECT 1 FROM history_upgrades WHERE name='manual-snapshots'").fetchone()
        before = pool.points_of(pool.linked_held_points())
    assert bundle.available_transfer_dates(net, md, cfg=cfg)
    with Pool(md, read_only=True) as pool:
        assert pool.points_of(pool.linked_held_points()) == before


def test_cli_preview_performs_upgrade_before_any_sync(old_history):
    cfg, net, md, _ = old_history
    result = CliRunner().invoke(app, ['--config', str(md.parent), 'bundle', '-n', net.name, '--dry-run'])
    assert result.exit_code == 0, result.output
    assert bundle.available_transfer_dates(net, md, cfg=cfg)


def test_opening_tui_transfer_first_offers_migrated_dates(old_history):
    from textual.widgets import Select
    from test_snapshot_tui import _run
    from waiting import until
    cfg, net, md, _ = old_history
    net = net.model_copy(update={'bundle_dir': md.parent / 'usb'})
    cfg = cfg.model_copy(update={'networks': [net]})
    async def check(tui, pilot):
        await pilot.press('t')
        await until(pilot, lambda: type(tui.screen).__name__ == 'BundlePickPage'
                    and tui.screen.query('#pick-to') and tui.screen.query_one('#pick-to').is_mounted)
        choices = {v for _, v in tui.screen.query_one('#pick-to', Select)._options}
        assert any(str(value).startswith('point:') for value in choices)
    _run(cfg, md.parent, check)


def test_interrupted_upgrade_is_finished_by_history_read(old_history):
    cfg, _, md, _ = old_history
    # Creating a table isn't proof its conversion completed.
    with Pool(md, read_only=True) as pool:
        db = pool.db_path
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE history_upgrades(name TEXT PRIMARY KEY)')
    rows = history.prepare(cfg)
    assert rows
    with Pool(md, read_only=True) as pool:
        assert pool._conn.execute("SELECT 1 FROM history_upgrades WHERE name='manual-snapshots'").fetchone()


def test_existing_repository_points_survive_151_schema_upgrade(tmp_path, monkeypatch):
    from test_repository_points import apt_repository
    cfg, md, units = apt_repository(tmp_path)
    points.record_points_after_sync(cfg, md, {units[0].source}, say=lambda _: None)
    with Pool(md, read_only=True) as pool:
        db, before = pool.db_path, pool.repository_points()
    # These are the schema additions since 1.5.1. There is deliberately no
    # network cache: the network is created after the recorded sync.
    with sqlite3.connect(db) as conn:
        for table in ('history_upgrades', 'published_points', 'published_legacy_holds'):
            conn.execute(f'DROP TABLE {table}')
    net = config.NetworkConfig(name='new', certs=False, nginx=False)
    cfg = cfg.model_copy(update={'networks': [net]})
    monkeypatch.setattr(points, 'record_points_after_sync', lambda *a, **kw: pytest.fail('synced'))
    assert bundle.available_transfer_dates(net, md, cfg=cfg) == ('20261101T000000Z',)
    assert history.prepare(cfg)
    with Pool(md, read_only=True) as pool:
        assert pool.repository_points() == before
        assert not pool.network_points(net.name)
        assert not pool.latest_recordings(net.name)


def test_empty_pool_is_not_created_by_history_preparation(tmp_path):
    md = tmp_path / 'missing'
    points.prepare_history(md)
    assert not md.exists()
