"""Findings from the sync-points branch's final review, each pinned."""
import hashlib

import pytest

from mmw.core import config, points
from mmw.core.pool import Pool
from mmw.site.tracks import PointDraft

from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply_timeline import set_a


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _old_package(pool):
    data = b"superseded package"
    cs = _sha(data)
    pool.add(data, cs, "sha256", "old.deb", len(data))
    return cs


def _draft(day, files):
    return PointDraft("h/dists/sid", f"release:{day}", f"202611{day:02d}T000000Z", {"Suite": "sid"}, files)


# 1 ── deleting a snapshot never strips a held point's files

def test_deleting_a_snapshot_keeps_held_points_files(tmp_path):
    with Pool(tmp_path) as pool:
        cs = _old_package(pool)
        pid, _ = pool.record_point("R", _draft(1, {"h/pool/old.deb": cs}), "s")
        pool.link_network_point("oak", pid, "s")
        sid = pool.create_snapshot("oak", "20261101T000000Z", kind="manual", rows=[("x", "y")])
        pool.link_snapshot_points(sid, [pid])
        pool.delete_snapshot(sid)
        assert pool.point_files(pid) == {"h/pool/old.deb": cs}
        assert pool.prune_preview(["S"]) == (0, 0)
        assert pool.snapshot_point_ids(sid) == []


# 2 ── prune drops points only once it is going ahead, under its lock; a dry run changes nothing

def _five_points(md, network):
    with Pool(md) as pool:
        for d in range(1, 6):
            pid, _ = pool.record_point("R", _draft(d, {"h/dists/sid/Release": f"c{d}"}), f"202611{d:02d}T1200Z")
            pool.link_network_point(network, pid, f"202611{d:02d}T120000Z")


def test_prune_answered_no_drops_nothing(two_mirrors, tmp_path, monkeypatch):  # noqa: F811
    from mmw.commands import prune
    cfg, net, md = two_mirrors
    _five_points(md, net.name)
    monkeypatch.setattr(prune, "load_config_or_none", lambda d: cfg)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert prune.run(tmp_path) == 1
    with Pool(md, read_only=True) as pool:
        assert len(pool.network_points(net.name)) == 5


def test_prune_dry_run_never_opens_the_pool_writable(two_mirrors, tmp_path, monkeypatch):  # noqa: F811
    from mmw.commands import prune
    cfg, net, md = two_mirrors
    _five_points(md, net.name)
    monkeypatch.setattr(prune, "load_config_or_none", lambda d: cfg)
    opened = []
    real = Pool.__init__

    def spy(self, mirror_dir, read_only=False, **kw):
        opened.append(read_only)
        return real(self, mirror_dir, read_only=read_only, **kw)
    monkeypatch.setattr(Pool, "__init__", spy)
    prune.run(tmp_path, dry_run=True)
    assert opened and all(opened)


def test_tui_prune_while_busy_drops_nothing(two_mirrors, monkeypatch):  # noqa: F811
    from mmw.core import pool as pool_mod
    from mmw.tui.repositories import prune_pool
    cfg, net, md = two_mirrors
    _five_points(md, net.name)
    with pool_mod.shared_pool_lock(md), pytest.raises(pool_mod.PoolBusy):
        prune_pool(cfg)
    with Pool(md, read_only=True) as pool:
        assert len(pool.network_points(net.name)) == 5


# 3 ── an age keeps what a network is on, counted from when it last saw it

def test_age_keeps_the_current_point(tmp_path):
    with Pool(tmp_path) as pool:
        pid, _ = pool.record_point("R", _draft(1, {"h/dists/sid/Release": "c1"}), "20260901T000000Z")
        pool.link_network_point("oak", pid, "20260901T000000Z")
        pool.link_network_point("oak", pid, "20261114T000000Z")             # seen again by today's sync
        assert points.retention_plan(pool, [config.NetworkConfig(name="oak", keep_points="30d")],
                                     now="20261115T000000Z") == []


def test_age_always_keeps_a_tracks_newest(tmp_path):
    with Pool(tmp_path) as pool:
        pid, _ = pool.record_point("R", _draft(1, {"h/dists/sid/Release": "c1"}), "20260901T000000Z")
        pool.link_network_point("oak", pid, "20260901T000000Z")
        assert points.retention_plan(pool, [config.NetworkConfig(name="oak", keep_points="30d")],
                                     now="20261115T000000Z") == []


# 4 ── a history-only snapshot holds nothing

def test_a_demoted_snapshot_holds_no_points(tmp_path):
    with Pool(tmp_path) as pool:
        ids = []
        for d in range(1, 6):
            pid, _ = pool.record_point("R", _draft(d, {"h/dists/sid/Release": f"c{d}"}), f"s{d}")
            pool.link_network_point("oak", pid, f"202611{d:02d}T000000Z")
            ids.append(pid)
        sid = pool.create_snapshot("oak", "20261101T000000Z", kind="manual", rows=[])
        pool.link_snapshot_points(sid, [ids[0]])
        pool.demote_snapshot(sid)
        assert ids[0] in points.retention_plan(pool, [config.NetworkConfig(name="oak")], now="20261120T000000Z")


# 5 ── a network removed from the config keeps nothing

def test_points_of_a_removed_network_go(tmp_path):
    with Pool(tmp_path) as pool:
        pid, _ = pool.record_point("R", _draft(1, {"h/dists/sid/Release": "c1"}), "s")
        pool.link_network_point("gone", pid, "20261101T000000Z")
        assert points.retention_plan(pool, [config.NetworkConfig(name="oak")]) == [pid]


# 6 ── an unchanged repository isn't split again

def test_an_unchanged_repository_is_not_split_again(two_mirrors, monkeypatch):  # noqa: F811
    cfg, net, md = two_mirrors
    set_a(md, 1)
    points.record_network_points(net, cfg, md)
    calls = []
    import mmw.site.tracks as tracks
    real = tracks.split_tracks
    monkeypatch.setattr(tracks, "split_tracks", lambda *a, **k: calls.append(1) or real(*a, **k))
    points.record_network_points(net, cfg, md)
    assert calls == []
    set_a(md, 2)
    points.record_network_points(net, cfg, md)
    assert len(calls) == 1


# 7 ── a dropped point keeps its record, not its file list; coming back restores it

def test_a_stub_drops_its_file_list_and_refills(tmp_path):
    with Pool(tmp_path) as pool:
        files = {"h/dists/sid/Release": "c1", "h/pool/a.deb": "c2"}
        pid, _ = pool.record_point("R", _draft(1, files), "s")
        pool.stub_point(pid)
        assert pool.point_files(pid) == {}
        assert pool.record_point("R", _draft(1, files), "s2") == (pid, False)
        assert pool.point_files(pid) == files


# 8 ── the TUI's sync records points

def test_the_tui_sync_records_points(two_mirrors, monkeypatch):  # noqa: F811
    from mmw.tui.sync import points_notice
    cfg, net, md = two_mirrors
    set_a(md, 1)
    seen = []
    monkeypatch.setattr(points, "record_points_after_sync",
                        lambda c, m, synced, say=print: seen.append(synced) or say("oak: new point(s) — a"))
    mirrors = [config.MirrorConfig(name="m", provider="apt-flat", source="S", source_url="https://x/")]
    assert points_notice(cfg, mirrors) == "oak: new point(s) — a" and seen == [{"S"}]


# 9 ── a snapshot taken now lists its points

def test_a_new_snapshot_lists_its_points(two_mirrors):  # noqa: F811
    from mmw.core.snapshots import take_snapshot
    cfg, net, md = two_mirrors
    set_a(md, 1)
    snap = take_snapshot(net, cfg, md)
    with Pool(md, read_only=True) as pool:
        assert pool.snapshot_point_ids(snap.id)


# 10 ── keep_points takes no booleans and no absurd ages

@pytest.mark.parametrize("bad", [True, 3.0, "1000000d", 10_000_001])
def test_keep_points_bounds(bad):
    with pytest.raises(ValueError, match="keep_points"):
        config.NetworkConfig(name="oak", keep_points=bad)
