"""History from before points belongs to its repository: a network created
later can select and send it, sliced to its own selection."""
import json
import sqlite3
import tarfile

import pytest

from mmw.core import config
from mmw.core.pool import Pool
from mmw.core.publisher.bundle import bundle_network
from mmw.core.snapshots import take_snapshot
from mmw.core.transfer_ranges import available_transfer_dates
from test_repository_points import apt_repository


def _forget_points(md):
    """The pool as an instance from before points left it: snapshots only."""
    with Pool(md, read_only=True) as pool:
        db = pool.db_path
    with sqlite3.connect(db) as conn:
        for table in ('points', 'point_files', 'point_units', 'point_unit_files', 'repository_current',
                      'network_points', 'network_recordings', 'network_settings',
                      'points_converted', 'recordings_seeded', 'snapshot_points'):
            conn.execute(f'DELETE FROM {table}')
        for table in ('history_upgrades', 'published_points', 'point_scopes'):
            conn.execute(f'DROP TABLE {table}')


@pytest.fixture
def old(tmp_path):
    cfg, md, units = apt_repository(tmp_path)
    a = config.NetworkConfig(name='a', nginx=False, certs=False)
    cfg = cfg.model_copy(update={'networks': [a]})
    snap = take_snapshot(a, cfg, md, kind='manual')
    _forget_points(md)
    b = config.NetworkConfig(name='b', nginx=False, certs=False, bundle_dir=tmp_path / 'usb',
                             select=[config.NetworkSelect(source=units[0].source, arches=['arm64'])])
    cfg = cfg.model_copy(update={'networks': [a, b]})
    config.save(cfg, tmp_path)
    return cfg, md, units, snap, a, b


def _upgraded(md):
    with Pool(md) as pool:                 # opening runs the history upgrades
        return pool


def test_other_network_can_send_pre_points_history(old, tmp_path):
    cfg, md, units, _snap, _a, b = old
    _upgraded(md)
    assert '20261101T000000Z' in available_transfer_dates(b, md, cfg=cfg)
    result = bundle_network(b, cfg, md, out_path=tmp_path / 'b.tar', full=True)
    with tarfile.open(result.out_path) as tar:
        rows = [json.loads(ln) for ln in tar.extractfile('.mmw/manifest.jsonl').read().decode().splitlines()]
    paths = [row.get('p', '') for row in rows[1:]]
    assert any(p.endswith('/pool/arm64.deb') for p in paths)
    assert not any(p.endswith('/pool/amd64.deb') for p in paths)
    assert not any('binary-amd64' in p for p in paths)


def test_promoted_point_projects_like_a_synced_one(old):
    from mmw.core.repository_points import files_for_settings
    cfg, md, units, snap, a, _b = old
    with Pool(md) as pool:
        (promoted,) = pool.repository_points(units[0].source)
        assert not promoted.scoped
        settings = pool.snapshots(a.name)[0].settings
        assert files_for_settings(pool, promoted.id, settings) == dict(pool.snapshot_files(snap.id))


def test_promoted_and_synced_points_of_one_state_are_one(old):
    from mmw.core import points
    cfg, md, units, _snap, _a, _b = old
    _upgraded(md)
    points.record_points_after_sync(cfg, md, {units[0].source}, say=lambda _: None)
    with Pool(md, read_only=True) as pool:
        assert len(pool.repository_points(units[0].source)) == 1


def test_unreadable_legacy_point_stays_and_warns(old):
    from mmw.core.legacy_points import promote_legacy_points
    _cfg, md, units, snap, _a, _b = old
    with Pool(md, read_only=True) as pool:           # read-only: no upgrade yet
        index = next(cs for path, cs in pool.snapshot_files(snap.id) if path.endswith('binary-arm64/Packages'))
        pool.blob_file(index).unlink()
    import mmw.core.legacy_points as lp
    caught = []
    real = lp.promote_legacy_points
    lp.promote_legacy_points = lambda pool: caught.extend(real(pool)) or caught
    try:
        with Pool(md):
            pass
    finally:
        lp.promote_legacy_points = real
    assert any("kept as a's only — its indexes can't be read" in w for w in caught), caught
    with Pool(md, read_only=True) as pool:
        assert not pool.repository_points(units[0].source)
        assert any(p.scoped and p.held for p in pool.points_of(pool.linked_held_points()).values())
    with Pool(md) as pool:
        assert promote_legacy_points(pool) == []          # said once, at the upgrade


def test_promotion_runs_once(old):
    from mmw.core.legacy_points import promote_legacy_points
    _cfg, md, units, _snap, _a, _b = old
    with Pool(md) as pool:
        before = pool.repository_points(units[0].source, stubs=True)
        assert promote_legacy_points(pool) == []
        assert pool.repository_points(units[0].source, stubs=True) == before
