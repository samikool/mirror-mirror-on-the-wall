"""What each network was sent: the points listed as its own and the files
carried — migrated once from the per-repository versions of format 2."""
from mmw.core import points
from mmw.core.pool import Pool

from test_bundle_mmw import _mmw
from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply_timeline import set_a


def test_record_and_read_sent(tmp_path):
    with Pool(tmp_path) as pool:
        pool.record_sent("oak", [1, 2], ["c1", "c2"], "20261115T000000Z")
        pool.record_sent("oak", [2], ["c2"], "20261116T000000Z")            # idempotent
        assert pool.sent_point_ids("oak") == {1, 2} and pool.sent_point_ids("elm") == set()
        assert pool.sent_checksums("oak", ["c1", "c3"]) == {"c1"}


def test_migrated_records_from_format2_history(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    set_a(md, 1)
    from mmw.core.snapshots import take_snapshot
    snap = take_snapshot(net, cfg, md, kind="bundle")
    with Pool(md) as pool:                                  # as a format-2 bundle to it recorded
        versions = pool.snapshot_repo_versions(snap.id)
        pool.record_bundle(net.name, None, snap.stamp, "old.tar", 0, 0, 0,
                           repos={repo: (None, v.id) for repo, v in versions.items()})
    with Pool(md) as pool:
        assert points.migrate_sent(pool, net.name)
        sent = pool.sent_point_ids(net.name)
        snap = pool.snapshots(net.name)[0]
        assert sent and sent <= set(pool.snapshot_point_ids(snap.id))
        files = {c for pid in sent for c in pool.point_files(pid).values()}
        assert pool.sent_checksums(net.name, files) == files


def test_migration_runs_once(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    with Pool(md) as pool:
        assert points.migrate_sent(pool, net.name)                 # nothing sent yet: still marked
        assert not points.migrate_sent(pool, net.name)


def test_current_points(two_mirrors):  # noqa: F811
    cfg, net, md = two_mirrors
    set_a(md, 1)
    points.record_network_points(net, cfg, md)
    with Pool(md, read_only=True) as pool:
        cur = points.current_points(pool, net.name)
        assert set(cur) >= {"https://example.com/a", "https://example.com/b"}   # (no network files here)
        assert all(len(ids) == 1 for ids in cur.values())
