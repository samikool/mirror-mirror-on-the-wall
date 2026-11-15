"""Edges of bundles of points: history per repository across partial
bundles, snapshots and stubs as destinations, --from by label, and a
suite gone from a repository."""
import json
import tarfile

import pytest

from mmw.core import points
from mmw.core import snapshots as S
from mmw.core.pool import Pool
from mmw.core.publisher.bundle import bundle_network

from test_bundle_mmw import _change_b, _mmw
from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply_timeline import set_a
from test_snapshot_repo_versions import _change_a, _key


@pytest.fixture
def mmw_net(two_mirrors):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    set_a(md, 1)
    return cfg, net, md


def _clock(monkeypatch, *stamps):
    seq = iter(stamps)
    monkeypatch.setattr(S, "now_stamp", lambda: next(seq))


def _header(path):
    with tarfile.open(path) as tar:
        return json.loads(tar.extractfile(".mmw/manifest.jsonl").readline())


def _a_dates(header, cfg):
    return sorted(p["date"][:8] for p in header["points"][_key(cfg, "a")]["points"])


def test_a_partial_bundle_does_not_skip_other_repositories_history(mmw_net, tmp_path, monkeypatch):
    cfg, net, md = mmw_net
    net = net.model_copy(update={"history": True, "keep_points": 1})
    cfg = cfg.model_copy(update={"networks": [net]})
    _clock(monkeypatch, "20300101T000000Z", "20300104T000000Z", "20300105T000000Z")
    bundle_network(net, cfg, md, out_path=tmp_path / "1.tar")
    for d in (2, 3):
        set_a(md, d)
        points.record_network_points(net, cfg, md, stamp=f"2030010{d}T000000Z")
    _change_b(md)
    bundle_network(net, cfg, md, out_path=tmp_path / "2.tar", repos=[_key(cfg, "b")])
    with Pool(md, read_only=True) as pool:
        a_points = [p.id for t in pool.network_tracks(net.name) if t.repo == _key(cfg, "a")
                    for p in pool.network_points(net.name, t.id)
                    if p.id not in pool.sent_point_ids(net.name)]
        assert a_points
        assert not set(points.retention_plan(pool, [net])) & set(a_points)   # a's history is still due
    r = bundle_network(net, cfg, md, out_path=tmp_path / "3.tar")
    assert _a_dates(_header(r.out_path), cfg) == ["20261102", "20261103"]


def test_to_a_history_only_snapshot_is_refused(mmw_net, tmp_path):
    cfg, net, md = mmw_net
    snap = S.take_snapshot(net, cfg, md, kind="manual")
    with Pool(md) as pool:
        pool._conn.execute("UPDATE snapshots SET complete = 0 WHERE id = ?", (snap.id,))
        pool._conn.commit()
    with pytest.raises(ValueError, match="history only"):
        bundle_network(net, cfg, md, out_path=tmp_path / "1.tar", to_ref=snap.stamp)


def test_a_stub_point_is_never_listed(mmw_net, tmp_path):
    cfg, net, md = mmw_net
    snap = S.take_snapshot(net, cfg, md, kind="manual")
    with Pool(md) as pool:
        points.convert_snapshots(pool, net.name)
        pool.stub_point(pool.snapshot_point_ids(snap.id)[0])
    with pytest.raises(ValueError, match="no longer kept"):
        bundle_network(net, cfg, md, out_path=tmp_path / "1.tar", to_ref=snap.stamp)


def test_retention_keeps_the_current_point_whatever_its_date(mmw_net, tmp_path):
    cfg, net, md = mmw_net
    net = net.model_copy(update={"keep_points": 1})
    set_a(md, 3)
    points.record_network_points(net, cfg, md, stamp="20300101T000000Z")
    set_a(md, 2)                                        # upstream moved back a day
    points.record_network_points(net, cfg, md, stamp="20300102T000000Z")
    with Pool(md, read_only=True) as pool:
        current = {p for ids in points.current_points(pool, net.name).values() for p in ids}
        assert not set(points.retention_plan(pool, [net])) & current


def test_from_a_label_bounds_history_by_its_stamp(mmw_net, tmp_path):
    cfg, net, md = mmw_net
    snap = S.take_snapshot(net, cfg, md, kind="manual")
    with Pool(md) as pool:
        pool.rename_snapshot(snap.id, "base")
    set_a(md, 2)
    r = bundle_network(net, cfg, md, out_path=tmp_path / "1.tar", from_ref="base", history=True)
    assert r.previous == snap.stamp and _header(r.out_path)["previous"] == snap.stamp


def test_history_skips_a_suite_the_repository_no_longer_has(mmw_net, tmp_path):
    cfg, net, md = mmw_net
    net = net.model_copy(update={"history": True})
    cfg = cfg.model_copy(update={"networks": [net]})
    bundle_network(net, cfg, md, out_path=tmp_path / "1.tar")
    set_a(md, 2)
    points.record_network_points(net, cfg, md, stamp="20990101T000000Z")
    _change_a(md)                                       # its Release is gone: suite `stable` goes
    h = _header(bundle_network(net, cfg, md, out_path=tmp_path / "2.tar").out_path)
    assert {p["track"] for p in h["points"][_key(cfg, "a")]["points"]} != set() and not any(
        p["fields"].get("Suite") == "stable" for p in h["points"][_key(cfg, "a")]["points"])
