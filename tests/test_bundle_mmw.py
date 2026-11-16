"""Bundles for a far side running mmw: timeline points (format 3), each
file carried only once to a network, and a bundle may name only some
repositories."""
import hashlib
import json
import tarfile

import pytest

from mmw.core.pool import Pool
from mmw.core.publisher.bundle import bundle_network

from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture
from test_snapshot_repo_versions import _change_a, _key


def _mmw(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    return cfg.model_copy(update={"networks": [net]}), net, mirror_dir


def _change_b(mirror_dir):
    from mmw.core.pool import KIND_META, KIND_PKG
    from test_dry_run_totals import SHARED, _index, _sha
    files = {"shared.deb": SHARED, "b.deb": b"b" * 200, "b2.deb": b"B" * 70}
    with Pool(mirror_dir) as pool:
        refs = []
        for fname, data in files.items():
            pool.add(data, _sha(data), "sha256", fname, len(data))
            refs.append((_sha(data), fname, KIND_PKG))
        idx = _index(files)
        pool.add(idx, _sha(idx), "sha256", "Packages", len(idx))
        refs.append((_sha(idx), "Packages", KIND_META))
        pool.reconcile_refs("b", refs)


def _read(bundle):
    """(header, {repo: {path: checksum}}, network rows, {checksum} carried)."""
    with tarfile.open(bundle) as tar:
        lines = tar.extractfile(".mmw/manifest.jsonl").read().decode().splitlines()
        carried = {hashlib.sha256(tar.extractfile(m).read()).hexdigest()
                   for m in tar if m.isfile() and not m.name.startswith(".mmw/")}
        names = tar.getnames()
    header = json.loads(lines[0])
    per_repo, network = {}, {}
    for line in lines[1:]:
        row = json.loads(line)
        (per_repo.setdefault(row["r"], {}) if "r" in row else network)[row["p"]] = row["c"]
    return header, per_repo, network, carried, names


def _current_files(md, net):
    from mmw.core import points
    with Pool(md, read_only=True) as p:
        out = {}
        for repo, ids in points.current_points(p, net.name).items():
            for pid in ids:
                out.setdefault(repo, {}).update(p.point_files(pid))
        return out


def test_partial_then_full_chain(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    ka, kb = _key(cfg, "a"), _key(cfg, "b")
    full = bundle_network(net, cfg, md, out_path=tmp_path / "1", from_ref=None)
    _change_a(md)
    _change_b(md)
    part = bundle_network(net, cfg, md, out_path=tmp_path / "2", repos=[kb])
    assert [d.repo for d in part.repos] == [kb]
    nxt = bundle_network(net, cfg, md, out_path=tmp_path / "3")
    assert {d.repo for d in nxt.repos} == {ka, kb}                     # the whole selection, listed

    # Play the three bundles onto a far side that keeps every blob it was sent.
    far: dict[str, dict[str, str]] = {}
    have: set[str] = set()
    for r in (full, part, nxt):
        header, per_repo, _, carried, names = _read(r.out_path)
        assert header["format"] == 3 and ".mmw/delete.list" not in names
        assert set(per_repo) == set(header["points"])
        have |= carried
        far.update(per_repo)
    final = _current_files(md, net)
    for repo in (ka, kb):
        assert far[repo] == final[repo]
        assert set(final[repo].values()) <= have                       # every file arrived at some point


def test_unknown_repo_lists_valid(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    with pytest.raises(LookupError, match="example.com/a"):
        bundle_network(net, cfg, md, out_path=tmp_path, repos=["nope"])


def test_deleting_snapshots_never_makes_a_bundle_full(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    bundle_network(net, cfg, md, out_path=tmp_path / "1", from_ref=None)
    from mmw.core.snapshots import take_snapshot
    snap = take_snapshot(net, cfg, md)
    with Pool(md) as p:
        p.delete_snapshot(snap.id)
    _change_a(md)
    r = bundle_network(net, cfg, md, out_path=tmp_path / "2")
    _h, per_repo, _n, carried, _names = _read(r.out_path)
    assert not set(per_repo[_key(cfg, "b")].values()) & carried        # b is already there
    assert not any("in full" in w for w in r.warnings), r.warnings


def test_nothing_to_send_raises(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    bundle_network(net, cfg, md, out_path=tmp_path / "1", from_ref=None)
    with pytest.raises(ValueError, match="nothing to send"):
        bundle_network(net, cfg, md, out_path=tmp_path / "2", repos=[_key(cfg, "a")])


def test_mmw_manifest_has_repo_tags_and_records(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    r = bundle_network(net, cfg, md, out_path=tmp_path, from_ref=None)
    header, per_repo, network, _, names = _read(r.out_path)
    assert set(per_repo) == {_key(cfg, "a"), _key(cfg, "b")}
    assert header["points"][_key(cfg, "a")]["label"] == "a" and header["selection"] is True
    assert any(p.startswith("nginx/") for p in network)               # network-level rows stay untagged
    assert names[0] == ".mmw/manifest.jsonl"
    with Pool(md) as p:
        assert p.sent_point_ids(net.name) and p.bundles(net.name)


def test_command_prints_each_repository(two_mirrors, tmp_path, capsys):  # noqa: F811
    from mmw.commands import bundle as cmd
    from mmw.core import config
    cfg, net, md = _mmw(two_mirrors)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    config.save(cfg, cfg_dir)
    assert cmd.run(cfg_dir, network=net.name, out=tmp_path / "o", allow_incomplete=True, from_ref="none") == 0
    out = capsys.readouterr().out
    assert "  a  files new → " in out and "  b  files new → " in out


def test_second_partial_to_the_same_snapshot(two_mirrors, tmp_path):  # noqa: F811
    from mmw.core.snapshots import take_snapshot
    cfg, net, md = _mmw(two_mirrors)
    bundle_network(net, cfg, md, out_path=tmp_path / "1", from_ref=None)
    _change_a(md)
    _change_b(md)
    s2 = take_snapshot(net, cfg, md)
    bundle_network(net, cfg, md, out_path=tmp_path / "2", repos=[_key(cfg, "a")], to_ref=s2.stamp)
    r = bundle_network(net, cfg, md, out_path=tmp_path / "3", repos=[_key(cfg, "b")], to_ref=s2.stamp)
    assert [d.repo for d in r.repos] == [_key(cfg, "b")]
    _h, per_repo, _n, carried, _names = _read(r.out_path)
    assert set(per_repo) == {_key(cfg, "b")} and carried


def test_mmw_manifest_carries_settings(two_mirrors, tmp_path):  # noqa: F811
    from mmw.core.snapshots import take_snapshot
    cfg, net, md = _mmw(two_mirrors)
    r = bundle_network(net, cfg, md, out_path=tmp_path, from_ref=None)
    header, *_ = _read(r.out_path)
    assert header["settings"] == take_snapshot(net, cfg, md).settings          # the same walk
