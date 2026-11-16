"""Bundles for an mmw network carry timeline points (format 3, a selection):
each chosen repository's current points, plus — with history — every point
since the last bundle; only files the network was never sent travel."""
import io
import json
import tarfile
import threading

import pytest

from mmw.core import points
from mmw.core.pool import Pool
from mmw.core.publisher.bundle import bundle_network
from mmw.core.publisher.manifest import blob_member

from test_bundle_mmw import _mmw
from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply_timeline import set_a
from test_snapshot_repo_versions import _key


@pytest.fixture
def mmw_net(two_mirrors):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    set_a(md, 1)
    return cfg, net, md


def _open(path):
    with tarfile.open(path) as tar:
        names = tar.getnames()
        lines = tar.extractfile(".mmw/manifest.jsonl").read().decode().splitlines()
    return names, json.loads(lines[0]), [json.loads(x) for x in lines[1:]]


def _bundle(cfg, net, md, out, **kw):
    return bundle_network(net, cfg, md, out_path=out, **kw)


def _a_dates(header, cfg):
    return sorted(p["date"][:8] for p in header["points"][_key(cfg, "a")]["points"])


def test_mmw_bundle_is_format3_with_selection(mmw_net, tmp_path):
    cfg, net, md = mmw_net
    r = _bundle(cfg, net, md, tmp_path / "1.tar")
    names, header, rows = _open(r.out_path)
    assert header["format"] == 3 and header["selection"] is True
    assert set(header["points"]) == {_key(cfg, "a"), _key(cfg, "b")}
    assert ".mmw/apply.py" not in names and all(n == ".mmw/manifest.jsonl" or n.startswith(("blobs/", ".mmw/"))
                                                for n in names)
    assert any(r["p"].startswith("nginx/") and "r" not in r for r in rows)          # generated, network-level
    with Pool(md, read_only=True) as pool:
        assert pool.snapshots(net.name) == []                                        # no snapshot taken
    assert any(label == "a" for label, _s in [(x.label, x.suites) for x in r.repos])


def test_newest_lists_current_points(mmw_net, tmp_path):
    cfg, net, md = mmw_net
    _bundle(cfg, net, md, tmp_path / "1.tar")
    for d in (2, 3):
        set_a(md, d)
        points.record_network_points(net, cfg, md, stamp=f"2026111{d}T000000Z")      # a sync each
    _n, header, _rows = _open(_bundle(cfg, net, md, tmp_path / "2.tar").out_path)
    assert _a_dates(header, cfg) == ["20261103"]


def test_history_lists_every_point_since_last_bundle(mmw_net, tmp_path):
    cfg, net, md = mmw_net
    _bundle(cfg, net, md, tmp_path / "1.tar")
    for d in (2, 3):
        set_a(md, d)
        points.record_network_points(net, cfg, md, stamp=f"2099111{d}T000000Z")
    _n, header, _rows = _open(_bundle(cfg, net, md, tmp_path / "2.tar", history=True).out_path)
    assert _a_dates(header, cfg) == ["20261102", "20261103"]


def test_network_history_setting_and_override(mmw_net, tmp_path):
    cfg, net, md = mmw_net
    net = net.model_copy(update={"history": True})
    cfg = cfg.model_copy(update={"networks": [net]})
    _bundle(cfg, net, md, tmp_path / "1.tar")
    for d in (2, 3):
        set_a(md, d)
        points.record_network_points(net, cfg, md, stamp=f"2099111{d}T000000Z")
    _n, header, _r = _open(_bundle(cfg, net, md, tmp_path / "2.tar", history=False).out_path)
    assert _a_dates(header, cfg) == ["20261103"]


def test_an_unchanged_suite_is_listed_not_carried(mmw_net, tmp_path):
    cfg, net, md = mmw_net
    _bundle(cfg, net, md, tmp_path / "1.tar")
    set_a(md, 2)
    names, header, rows = _open(_bundle(cfg, net, md, tmp_path / "2.tar").out_path)
    b_rows = [r for r in rows if r.get("r") == _key(cfg, "b")]
    assert _key(cfg, "b") in header["points"] and b_rows
    assert not any(blob_member(r["c"]) in names for r in b_rows)


def test_first_format3_after_format2_is_a_delta(mmw_net, tmp_path):
    from mmw.core.snapshots import take_snapshot
    cfg, net, md = mmw_net
    snap = take_snapshot(net, cfg, md, kind="bundle")
    with Pool(md) as pool:                                  # as a format-2 bundle to it recorded
        versions = pool.snapshot_repo_versions(snap.id)
        pool.record_bundle(net.name, None, snap.stamp, "old.tar", 0, 0, 0,
                           repos={repo: (None, v.id) for repo, v in versions.items()})
    set_a(md, 2)
    names, _h, rows = _open(_bundle(cfg, net, md, tmp_path / "2.tar").out_path)
    shared = next(r["c"] for r in rows if r["p"].endswith("/shared.deb"))
    assert blob_member(shared) not in names                 # went with the format-2 bundle
    assert any(r["p"].endswith("/a2.deb") and blob_member(r["c"]) in names for r in rows)


def test_a_stopped_bundle_records_nothing(mmw_net, tmp_path):
    cfg, net, md = mmw_net
    stop = threading.Event()

    def progress(_rel, status):
        if str(status).startswith("archive-start"):
            stop.set()
    r = _bundle(cfg, net, md, tmp_path / "1.tar", progress=progress, stop_event=stop)
    assert r.own_errors and not (tmp_path / "1.tar").exists()
    with Pool(md, read_only=True) as pool:
        assert pool.sent_point_ids(net.name) == set() and pool.bundles(net.name) == []


def test_partial_bundle_records_only_its_repositories(mmw_net, tmp_path):
    cfg, net, md = mmw_net
    a = _key(cfg, "a")
    _n, header, _r = _open(_bundle(cfg, net, md, tmp_path / "1.tar", repos=[a]).out_path)
    assert set(header["points"]) == {a}
    with Pool(md, read_only=True) as pool:
        sent = pool.sent_point_ids(net.name)
        assert sent and all(pool.track_of_point(p).repo in (a, f"network:{net.name}") for p in sent)


def test_stage_and_stream_write_format3(mmw_net, tmp_path):
    cfg, net, md = mmw_net
    staged = bundle_network(net, cfg, md, out_path=tmp_path / "stage", stage=True)
    manifest = (staged.out_path / ".mmw/manifest.jsonl").read_text().splitlines()[0]
    assert json.loads(manifest)["format"] == 3
    set_a(md, 2)
    buf = io.BytesIO()
    r = bundle_network(net, cfg, md, out_path="-", stdout=buf)
    with tarfile.open(fileobj=io.BytesIO(buf.getvalue())) as tar:
        assert json.loads(tar.extractfile(".mmw/manifest.jsonl").readline())["format"] == 3
    assert r.stream_sha256


def test_full_carries_everything_and_nothing_new_says_so(mmw_net, tmp_path):
    cfg, net, md = mmw_net
    first, _h, rows = _open(_bundle(cfg, net, md, tmp_path / "1.tar").out_path)
    with pytest.raises(ValueError, match="nothing to send"):
        _bundle(cfg, net, md, tmp_path / "2.tar")
    again, _h2, _r2 = _open(_bundle(cfg, net, md, tmp_path / "3.tar", full=True).out_path)
    assert sorted(again) == sorted(first)


def test_progress_says_which_repository_is_being_bundled(mmw_net, tmp_path):
    cfg, net, md = mmw_net
    started = []
    bundle_network(net, cfg, md, out_path=tmp_path / "1.tar",
                   mirror_start_callback=lambda name, i, n, **kw: started.append((name, i, n)))
    assert [(i, n) for _name, i, n in started] == [(1, 2), (2, 2)]
    assert {name for name, _i, _n in started} == {"example.com/a", "example.com/b"}


def test_a_network_with_no_files_of_its_own_still_bundles_its_nginx(mmw_net, tmp_path):
    """No keys, nothing outside a repository: no `network:` track — and the
    bundle still carries the network's generated nginx."""
    cfg, net, md = mmw_net
    points.record_network_points(net, cfg, md)
    with Pool(md, read_only=True) as pool:
        assert not [t for t in pool.network_tracks(net.name) if t.repo.startswith("network:")]
    _names, _header, rows = _open(_bundle(cfg, net, md, tmp_path / "1.tar").out_path)
    assert any(r["p"] == "nginx/mirror.conf" and "r" not in r for r in rows)


def test_a_network_with_keys_gets_its_network_track_and_bundles_them(mmw_net, tmp_path):
    cfg, net, md = mmw_net
    (md / "keys" / "custom").mkdir(parents=True)
    (md / "keys" / "custom" / "archive.gpg").write_bytes(b"a public key")
    points.record_network_points(net, cfg, md)
    with Pool(md, read_only=True) as pool:
        mine = [t for t in pool.network_tracks(net.name) if t.repo == f"network:{net.name}"]
        assert len(mine) == 1
    _names, _header, rows = _open(_bundle(cfg, net, md, tmp_path / "1.tar").out_path)
    assert any(r["p"] == "keys/custom/archive.gpg" for r in rows)
    assert any(r["p"] == "nginx/mirror.conf" for r in rows)
