"""Keeping the newest points per suite: older ones become stubs (their record,
not their files) when prune runs — by the repository's rule, never one a
snapshot holds or one a history network hasn't been sent since its last
bundle. Networks have no retention of their own."""
import pytest

from mmw.core import config, points
from mmw.core.pool import Pool
from mmw.site.tracks import PointDraft

from test_dry_run_totals import two_mirrors  # noqa: F401

REPO = "https://h.test/debian"


def _draft(day: int, files=None):
    return PointDraft("h/dists/sid", f"release:{day}", f"202611{day:02d}T000000Z", {"Suite": "sid"},
                      files or {"h/dists/sid/Release": f"c{day}"})


def _cfg(tmp_path, keep=3):
    return config.Config(settings=config.Settings(mirror_dir=tmp_path, keep_points=keep),
                         sources=[config.SourceConfig(url=REPO)])


def _seed(pool, days, network=None, repo=REPO, legacy=False):
    ids = {}
    for d in days:
        pid, _ = pool.record_point(repo, _draft(d), f"202611{d:02d}T120000Z", legacy=legacy)
        if network:
            pool.link_network_point(network, pid, f"202611{d:02d}T120000Z")
        ids[d] = pid
    return ids


def _kept(pool, repo=REPO):
    return sorted(int(p.ident.split(":")[1]) for p in pool.repository_points(repo))


def test_keeps_newest_three_per_track(tmp_path):
    with Pool(tmp_path) as pool:
        _seed(pool, range(1, 7))
        gone = points.apply_retention(pool, [], now="20261120T000000Z", cfg=_cfg(tmp_path))
        assert gone == 3 and _kept(pool) == [4, 5, 6]


@pytest.mark.parametrize("keep,left", [("all", [1, 2, 3, 4, 5, 6]), (1, [6]), ("10d", [3, 4, 5, 6])])
def test_keep_points_setting(tmp_path, keep, left):
    with Pool(tmp_path) as pool:
        _seed(pool, range(1, 7))
        points.apply_retention(pool, [], now="20261114T000000Z", cfg=_cfg(tmp_path, keep))
        assert _kept(pool) == left


def test_network_walk_points_stay_while_their_network_does(tmp_path):
    cfg = _cfg(tmp_path, 1)
    net = config.NetworkConfig(name="oak")
    with Pool(tmp_path) as pool:
        ids = _seed(pool, range(1, 6), network=net.name, repo="R", legacy=True)
        assert points.retention_plan(pool, [net], now="20261120T000000Z", cfg=cfg) == []
        assert sorted(points.retention_plan(pool, [], now="20261120T000000Z", cfg=cfg)) == sorted(ids.values())


def test_snapshot_and_unsent_points_are_kept(tmp_path):
    net = config.NetworkConfig(name="oak", history=True)
    with Pool(tmp_path) as pool:
        ids = _seed(pool, range(1, 7), network=net.name)
        sid = pool.create_snapshot("oak", "20261102T130000Z", kind="manual", rows=[])
        pool.link_snapshot_points(sid, [ids[2]])                          # a snapshot holds day 2
        pool.record_bundle("oak", None, "20261102T130000Z", "x.tar", 0, 0, 0)   # last bundle after day 2 …
        points.apply_retention(pool, [net], now="20261120T000000Z", cfg=_cfg(tmp_path, 1))
        assert _kept(pool) == [2, 3, 4, 5, 6]                           # … so days 3–5 haven't gone yet


def test_bad_keep_points_is_refused_in_config(tmp_path):
    with pytest.raises(ValueError, match="keep_points"):
        config.Settings(mirror_dir=tmp_path, keep_points="soon")


def _three(two_mirrors):  # noqa: F811
    cfg, net, md = two_mirrors
    cfg = cfg.model_copy(update={"settings": cfg.settings.model_copy(update={"keep_points": 3})})
    return cfg, net, md, cfg.sources[0].identity


def test_prune_keeps_held_points_and_frees_dropped_ones(two_mirrors):  # noqa: F811
    import hashlib
    cfg, net, md, repo = _three(two_mirrors)
    with Pool(md) as pool:
        data = b"superseded"
        cs = hashlib.sha256(data).hexdigest()
        pool.add(data, cs, "sha256", "old.deb", len(data))
        for d in range(1, 5):
            files = {"h/dists/sid/Release": f"c{d}", **({"h/pool/old.deb": cs} if d == 1 else {})}
            pid, _ = pool.record_point(repo, _draft(d, files), f"202611{d:02d}T120000Z")
            pool.link_network_point(net.name, pid, f"202611{d:02d}T120000Z")
        sources = [s.identity for s in cfg.sources]
        assert pool.prune_preview(sources)[0] == 0                       # day 1 still holds it
        points.apply_retention(pool, [net], now="20261120T000000Z", cfg=cfg)
        assert pool.prune_preview(sources)[0] == 1                       # stubbed: its blob may go


def test_prune_command_runs_retention_first(two_mirrors, monkeypatch, tmp_path, capsys):  # noqa: F811
    from mmw.commands import prune
    cfg, net, md, repo = _three(two_mirrors)
    with Pool(md) as pool:
        _seed(pool, range(1, 6), network=net.name, repo=repo)
    monkeypatch.setattr(prune, "load_config_or_none", lambda d: cfg)
    prune.run(tmp_path, dry_run=True)
    assert "2 old point(s) no retention rule keeps (keep_points) would be dropped" in capsys.readouterr().out
    with Pool(md, read_only=True) as pool:
        assert len(pool.repository_points(repo)) == 5                # a dry run drops nothing
    prune.run(tmp_path, yes=True)
    with Pool(md, read_only=True) as pool:
        assert len(pool.repository_points(repo)) == 3


def test_the_tuis_prune_drops_old_points_too(two_mirrors):  # noqa: F811
    from mmw.tui.repositories import prune_pool
    cfg, net, md, repo = _three(two_mirrors)
    with Pool(md) as pool:
        _seed(pool, range(1, 6), network=net.name, repo=repo)
    prune_pool(cfg)
    with Pool(md, read_only=True) as pool:
        assert len(pool.repository_points(repo)) == 3
