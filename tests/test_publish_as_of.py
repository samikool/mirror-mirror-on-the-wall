"""Publishing a network as of a date: each repository as the network last
recorded it at or before then — the tree a publish laid out on that day."""
import json

import pytest

from mmw.core import points
from mmw.core import config
from mmw.core.pool import Pool
from mmw.core.publisher import publish_as_of, publish_network

from test_bundle_snapshots import SRC, _state, _tree


@pytest.fixture
def recorded(tmp_path):
    """Network `lab` recorded on 1, 2 and 3 Jan 2030, and what a publish laid out each day."""
    mirror_dir = tmp_path / "mirror"
    cfg = config.Config(settings=config.Settings(mirror_dir=mirror_dir),
                        sources=[config.SourceConfig(url=SRC, suites=["noble"],
                                                     components=["main"], arches=["amd64"])])
    net = config.NetworkConfig(name="lab", certs=False)
    published = {}
    for v in (1, 2, 3):
        _state(mirror_dir, v)
        points.record_network_points(net, cfg, mirror_dir, stamp=f"2030010{v}T000000Z")
        root = tmp_path / f"published-{v}"
        publish_network(net.model_copy(update={"root": root}), cfg, mirror_dir)
        published[v] = _tree(root)
    with Pool(mirror_dir) as pool:
        pool.prune_orphans([SRC])                 # held points keep their files
    return cfg, net, mirror_dir, published


def _files(tree):
    return {p: b for p, b in tree.items() if "/by-hash/" not in p}


def test_as_of_publishes_what_the_network_had_then(recorded, tmp_path):
    cfg, net, md, published = recorded
    for when, v in (("2030-01-01", 1), ("20300102T120000Z", 2), ("2030-01-03", 3)):
        root = tmp_path / f"as-of-{v}"
        publish_as_of(net.model_copy(update={"root": root}), cfg, md, when)
        assert _files(_tree(root)) == _files(published[v]), when


def test_as_of_now_matches_live_publish(recorded, tmp_path):
    cfg, net, md, published = recorded
    root = tmp_path / "now"
    publish_as_of(net.model_copy(update={"root": root}), cfg, md, "29991231T000000Z")
    assert _files(_tree(root)) == _files(published[3])


def test_as_of_before_the_first_recording_is_refused(recorded, tmp_path):
    cfg, net, md, _p = recorded
    with pytest.raises(ValueError, match="nothing recorded for lab at or before.*2030-01-01"):
        publish_as_of(net.model_copy(update={"root": tmp_path / "x"}), cfg, md, "2029-12-31")


def test_the_published_date_is_recorded(recorded, tmp_path):
    cfg, net, md, _p = recorded
    root = tmp_path / "x"
    publish_as_of(net.model_copy(update={"root": root}), cfg, md, "2030-01-01")
    with Pool(md, read_only=True) as pool:
        assert {p.root: p.target for p in pool.published("lab")}[str(root.resolve())] \
            == "as-of:20300101T235959Z"


def test_as_of_uses_that_dates_settings(recorded, tmp_path):
    cfg, net, md, _p = recorded
    with Pool(md) as pool:
        first = pool.settings_at("lab", "20300101T000000Z")
        changed = json.dumps({**json.loads(first), "marker": "later"})
        pool.record_settings("lab", "20300102T000000Z", changed)
        assert pool.settings_at("lab", "20300101T235959Z") == first
        assert pool.settings_at("lab", "20300102T000000Z") == changed


def test_retention_keeps_a_published_moment(recorded, tmp_path):
    cfg, net, md, _p = recorded
    cfg = cfg.model_copy(update={"settings": cfg.settings.model_copy(update={"keep_points": 1})})
    publish_as_of(net.model_copy(update={"root": tmp_path / "x"}), cfg, md, "2030-01-01")
    with Pool(md, read_only=True) as pool:
        moment = {p for ids in pool.points_at("lab", "20300101T235959Z").values() for p in ids}
        assert moment and not set(points.retention_plan(pool, [net], cfg=cfg)) & moment


def test_a_stubbed_moment_is_refused(recorded, tmp_path):
    cfg, net, md, _p = recorded
    with Pool(md) as pool:
        for ids in pool.points_at("lab", "20300101T235959Z").values():
            for pid in ids:
                if pid not in {p for i in pool.points_at("lab", "20300103T000000Z").values() for p in i}:
                    pool.stub_point(pid)
    with pytest.raises(ValueError, match="no longer kept.*keep_points"):
        publish_as_of(net.model_copy(update={"root": tmp_path / "x"}), cfg, md, "2030-01-01")
