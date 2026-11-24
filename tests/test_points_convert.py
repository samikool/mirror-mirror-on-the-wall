"""Existing snapshots become points once: each one's files split like a
sync's, linked to its network at its own stamp, and listed for the snapshot."""
from mmw.core import legacy_points
from mmw.core import points
from mmw.core.pool import Pool
from mmw.core.snapshots import take_snapshot

from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply_timeline import set_a


def test_snapshots_become_points_once(two_mirrors):  # noqa: F811
    cfg, net, md = two_mirrors
    set_a(md, 1)
    s1 = take_snapshot(net, cfg, md)
    set_a(md, 2)
    s2 = take_snapshot(net, cfg, md)
    with Pool(md) as pool:
        assert legacy_points.convert_snapshots(pool, net.name) == 2
        assert legacy_points.convert_snapshots(pool, net.name) == 0
        a_track = next(t for t in pool.network_tracks(net.name) if t.name == "example.com/a")
        dated = pool.network_points(net.name, a_track.id)
        assert [p.date[:8] for p in dated] == ["20261102", "20261101"]
        assert {pool.network_point_stamp(net.name, p.id) for p in dated} == {s1.stamp, s2.stamp}
        assert set(pool.snapshot_point_ids(s2.id)) >= {dated[0].id}


def test_history_only_snapshots_are_skipped(two_mirrors):  # noqa: F811
    cfg, net, md = two_mirrors
    with Pool(md) as pool:
        pool.create_snapshot(net.name, "20260101T000000Z", kind="imported", complete=False, rows=[("x", "c")])
        assert legacy_points.convert_snapshots(pool, net.name) == 0
        assert pool.network_points(net.name) == []


def test_recording_converts_first(two_mirrors):  # noqa: F811
    cfg, net, md = two_mirrors
    set_a(md, 1)
    snap = take_snapshot(net, cfg, md)
    points.record_network_points(net, cfg, md, stamp="20991231T000000Z")
    with Pool(md, read_only=True) as pool:
        a = next(t for t in pool.network_tracks(net.name) if t.name == "example.com/a")
        (p,) = pool.network_points(net.name, a.id)       # the snapshot's point is the sync's
        assert pool.network_point_stamp(net.name, p.id) == snap.stamp      # first seen then


def test_conversion_leaves_bundles_alone(two_mirrors, tmp_path):  # noqa: F811
    import tarfile
    from mmw.core.publisher.bundle import bundle_network
    cfg, net, md = two_mirrors
    set_a(md, 1)
    snap = take_snapshot(net, cfg, md)

    def names(out):
        with tarfile.open(bundle_network(net, cfg, md, out_path=out, from_ref=None, to_ref=snap.stamp).out_path) as t:
            return sorted(t.getnames())
    before = names(tmp_path / "1.tar")
    with Pool(md) as pool:
        legacy_points.convert_snapshots(pool, net.name)
    assert names(tmp_path / "2.tar") == before


def test_one_snapshot_that_fails_to_split_is_said_and_skipped(two_mirrors, monkeypatch):  # noqa: F811
    """One bad old snapshot never stops a network's points on every sync."""
    cfg, net, md = two_mirrors
    set_a(md, 1)
    take_snapshot(net, cfg, md)
    set_a(md, 2)
    bad = take_snapshot(net, cfg, md)
    real = points.split_rows

    def split(pool, rows, settings, network, stamp):
        if stamp == bad.stamp:
            raise ValueError("a damaged Release")
        return real(pool, rows, settings, network, stamp)
    monkeypatch.setattr(points, "split_rows", split)
    said = []
    with Pool(md) as pool:
        assert legacy_points.convert_snapshots(pool, net.name, warnings=said) == 1
        assert pool.points_converted(net.name)
    assert len(said) == 1 and bad.stamp in said[0] and "a damaged Release" in said[0]
