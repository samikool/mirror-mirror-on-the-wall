"""Bundles for a far side running mmw: each repository's delta runs from
the version last sent to it, and a bundle may carry only some of them."""
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
    net = net.model_copy(update={"far_side": "mmw"})
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


def test_partial_then_full_chain(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    ka, kb = _key(cfg, "a"), _key(cfg, "b")
    full = bundle_network(net, cfg, md, out_path=tmp_path / "1", from_ref=None)
    _change_a(md)
    _change_b(md)
    part = bundle_network(net, cfg, md, out_path=tmp_path / "2", repos=[kb])
    assert [d.repo for d in part.repos] == [kb]
    nxt = bundle_network(net, cfg, md, out_path=tmp_path / "3")
    by = {d.repo: d for d in nxt.repos}
    assert kb not in by                                                # already sent
    assert (by[ka].start.number, by[ka].dest.number) == (1, 2)          # a from what was last sent to it

    # Play the three bundles onto a far side that keeps every blob it was sent.
    far: dict[str, dict[str, str]] = {}
    have: set[str] = set()
    for r in (full, part, nxt):
        header, per_repo, _, carried, names = _read(r.out_path)
        assert header["format"] == 2 and ".mmw/delete.list" not in names
        assert set(per_repo) == set(header["repos"])
        have |= carried
        far.update(per_repo)
    with Pool(md) as p:
        final = dict(p.snapshot_files(p.snapshots(net.name)[0].id))
    for repo, base in ((ka, "example.com/a/"), (kb, "example.com/b/")):
        want = {k: c for k, c in final.items() if k.startswith(base)}
        assert far[repo] == want
        assert set(want.values()) <= have                              # every file arrived at some point


def test_partial_refused_on_apply_network(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = two_mirrors
    with pytest.raises(ValueError, match="far_side"):
        bundle_network(net, cfg, md, out_path=tmp_path, repos=[_key(cfg, "a")])


def test_unknown_repo_lists_valid(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    with pytest.raises(LookupError, match="example.com/a"):
        bundle_network(net, cfg, md, out_path=tmp_path, repos=["nope"])


def test_mmw_bundle_from_imported_falls_back_to_full_with_warning(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    with Pool(md) as p:
        p.create_snapshot(net.name, "20200101T000000Z", kind="imported", complete=False,
                          rows=[("example.com/a/a.deb", "x")])
    r = bundle_network(net, cfg, md, out_path=tmp_path, from_ref="20200101T000000Z")
    assert all(d.start is None for d in r.repos)
    assert any("history only" in w and "in full" in w for w in r.warnings)


def test_deleted_start_version_falls_back_to_full(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    first = bundle_network(net, cfg, md, out_path=tmp_path / "1", from_ref=None)
    _change_a(md)
    from mmw.core.snapshots import take_snapshot
    take_snapshot(net, cfg, md)
    with Pool(md) as p:
        p.delete_snapshot(next(s.id for s in p.snapshots(net.name) if s.stamp == first.created))
    r = bundle_network(net, cfg, md, out_path=tmp_path / "2")
    by = {d.repo: d for d in r.repos}
    assert by[_key(cfg, "a")].start is None
    assert any("no longer kept" in w for w in r.warnings)


def test_nothing_to_send_raises(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    bundle_network(net, cfg, md, out_path=tmp_path / "1", from_ref=None)
    with pytest.raises(ValueError, match="nothing to send"):
        bundle_network(net, cfg, md, out_path=tmp_path / "2", repos=[_key(cfg, "a")])


def test_mmw_manifest_has_repo_tags_and_ledger_rows(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    r = bundle_network(net, cfg, md, out_path=tmp_path, from_ref=None)
    header, per_repo, network, _, names = _read(r.out_path)
    assert set(per_repo) == {_key(cfg, "a"), _key(cfg, "b")}
    assert header["repos"][_key(cfg, "a")]["from"] is None and header["repos"][_key(cfg, "a")]["to"] == 1
    assert header["repos"][_key(cfg, "a")]["label"] == "a"
    assert "apply.py" in network                                      # network-level rows stay untagged
    assert names[0] == ".mmw/manifest.jsonl"
    with Pool(md) as p:
        assert set(p.last_sent_versions(net.name)) == {_key(cfg, "a"), _key(cfg, "b")}


def test_command_prints_each_repository(two_mirrors, tmp_path, capsys):  # noqa: F811
    from mmw.commands import bundle as cmd
    from mmw.core import config
    cfg, net, md = _mmw(two_mirrors)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    config.save(cfg, cfg_dir)
    assert cmd.run(cfg_dir, network=net.name, out=tmp_path / "o", allow_incomplete=True, from_ref="none") == 0
    out = capsys.readouterr().out
    assert "  a  full → v1" in out and "  b  full → v1" in out


def test_second_partial_to_the_same_snapshot(two_mirrors, tmp_path):  # noqa: F811
    from mmw.core.snapshots import take_snapshot
    cfg, net, md = _mmw(two_mirrors)
    bundle_network(net, cfg, md, out_path=tmp_path / "1", from_ref=None)
    _change_a(md)
    _change_b(md)
    s2 = take_snapshot(net, cfg, md)
    bundle_network(net, cfg, md, out_path=tmp_path / "2", repos=[_key(cfg, "a")], to_ref=s2.stamp)
    r = bundle_network(net, cfg, md, out_path=tmp_path / "3", repos=[_key(cfg, "b")], to_ref=s2.stamp)
    assert [(d.repo, d.start.number, d.dest.number) for d in r.repos] == [(_key(cfg, "b"), 1, 2)]


def test_mmw_manifest_carries_settings(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    r = bundle_network(net, cfg, md, out_path=tmp_path, from_ref=None)
    header, *_ = _read(r.out_path)
    with Pool(md) as p:
        dest = p.snapshots(net.name)[0]
    assert header["settings"] == dest.settings
