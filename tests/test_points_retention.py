"""Keeping the newest points per suite: older ones become stubs (their record,
not their files) when prune runs — never one a snapshot holds, one a network
hasn't been sent since its last bundle, or one another network keeps."""
import pytest

from mmw.core import config, points
from mmw.core.pool import Pool
from mmw.site.tracks import PointDraft

from test_dry_run_totals import two_mirrors  # noqa: F401


def _draft(day: int, files=None):
    return PointDraft("h/dists/sid", f"release:{day}", f"202611{day:02d}T000000Z", {"Suite": "sid"},
                      files or {"h/dists/sid/Release": f"c{day}"})


def _net(**kw):
    return config.NetworkConfig(name="oak", **kw)


def _seed(pool, days, network="oak"):
    ids = {}
    for d in days:
        pid, _ = pool.record_point("R", _draft(d), f"202611{d:02d}T120000Z")
        pool.link_network_point(network, pid, f"202611{d:02d}T120000Z")
        ids[d] = pid
    return ids


def _kept(pool, network="oak"):
    return sorted(int(p.ident.split(":")[1]) for p in pool.network_points(network))


def test_keeps_newest_three_per_track(tmp_path):
    with Pool(tmp_path) as pool:
        _seed(pool, range(1, 7))
        gone = points.apply_retention(pool, [_net()], now="20261120T000000Z")
        assert gone == 3 and _kept(pool) == [4, 5, 6]


@pytest.mark.parametrize("keep,left", [("all", [1, 2, 3, 4, 5, 6]), (1, [6]), ("10d", [2, 3, 4, 5, 6])])
def test_keep_points_setting(tmp_path, keep, left):
    with Pool(tmp_path) as pool:
        _seed(pool, range(1, 7))
        points.apply_retention(pool, [_net(keep_points=keep)], now="20261112T000000Z")
        assert _kept(pool) == left


def test_a_point_another_network_keeps_stays(tmp_path):
    with Pool(tmp_path) as pool:
        ids = _seed(pool, range(1, 6))
        pool.link_network_point("elm", ids[1], "20261101T120000Z")       # elm's only point
        points.apply_retention(pool, [_net(), config.NetworkConfig(name="elm")], now="20261120T000000Z")
        assert 1 in _kept(pool) and 2 not in _kept(pool)


def test_snapshot_and_unsent_points_are_kept(tmp_path):
    with Pool(tmp_path) as pool:
        ids = _seed(pool, range(1, 7))
        sid = pool.create_snapshot("oak", "20261102T130000Z", kind="manual", rows=[])
        pool.link_snapshot_points(sid, [ids[2]])                          # a snapshot holds day 2
        pool.record_bundle("oak", None, "20261102T130000Z", "x.tar", 0, 0, 0)   # last bundle after day 2 …
        points.apply_retention(pool, [_net(keep_points=1)], now="20261120T000000Z")
        assert _kept(pool) == [2, 3, 4, 5, 6]                           # … so days 3–5 haven't gone yet


def test_bad_keep_points_is_refused_in_config():
    with pytest.raises(ValueError, match="keep_points"):
        config.NetworkConfig(name="oak", keep_points="soon")


def test_prune_keeps_held_points_and_frees_dropped_ones(two_mirrors):  # noqa: F811
    import hashlib
    cfg, net, md = two_mirrors
    with Pool(md) as pool:
        data = b"superseded"
        cs = hashlib.sha256(data).hexdigest()
        pool.add(data, cs, "sha256", "old.deb", len(data))
        for d in range(1, 5):
            files = {"h/dists/sid/Release": f"c{d}", **({"h/pool/old.deb": cs} if d == 1 else {})}
            pid, _ = pool.record_point("R", _draft(d, files), f"202611{d:02d}T120000Z")
            pool.link_network_point(net.name, pid, f"202611{d:02d}T120000Z")
        sources = [s.identity for s in cfg.sources]
        assert pool.prune_preview(sources)[0] == 0                       # day 1 still holds it
        points.apply_retention(pool, [net], now="20261120T000000Z")
        assert pool.prune_preview(sources)[0] == 1                       # stubbed: its blob may go


def test_prune_command_runs_retention_first(two_mirrors, monkeypatch, tmp_path, capsys):  # noqa: F811
    from mmw.commands import prune
    cfg, net, md = two_mirrors
    with Pool(md) as pool:
        _seed(pool, range(1, 6), network=net.name)
    monkeypatch.setattr(prune, "load_config_or_none", lambda d: cfg)
    prune.run(tmp_path, dry_run=True)
    assert "2 old point(s) no retention rule keeps (keep_points) would be dropped" in capsys.readouterr().out
    with Pool(md, read_only=True) as pool:
        assert len(pool.network_points(net.name)) == 5                # a dry run drops nothing
    prune.run(tmp_path, yes=True)
    with Pool(md, read_only=True) as pool:
        assert len(pool.network_points(net.name)) == 3


def test_the_tuis_prune_drops_old_points_too(two_mirrors):  # noqa: F811
    from mmw.tui.repositories import prune_pool
    cfg, net, md = two_mirrors
    with Pool(md) as pool:
        _seed(pool, range(1, 6), network=net.name)
    prune_pool(cfg)
    with Pool(md, read_only=True) as pool:
        assert len(pool.network_points(net.name)) == 3


@pytest.mark.parametrize("text,value", [("", 3), ("3", 3), (" 12 ", 12), ("all", "all"), ("90d", "90d")])
def test_keep_points_as_typed_in_the_tui(text, value):
    from mmw.tui.network_screens import parse_keep_points
    assert parse_keep_points(text) == value


@pytest.mark.parametrize("text", ["0", "soon", "-1", "3x"])
def test_bad_keep_points_in_the_tui(text):
    from mmw.tui.network_screens import parse_keep_points
    with pytest.raises(ValueError, match="keep"):
        parse_keep_points(text)
