"""`mmw stage`: a bundle laid out as a directory of hardlinks — the same
members, costing almost no disk, there to tar, rsync or inspect without mmw."""
import io
import os
import tarfile
from pathlib import Path

import pytest

from mmw.core import disk_io
from mmw.core.pool import Pool
from mmw.core.publisher import bundle as bundle_mod
from mmw.core.publisher.bundle import bundle_network, stage_network

from bundle_golden import describe
from test_bundle_mmw import _mmw
from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def _members(bundle: Path) -> dict[str, bytes]:
    with tarfile.open(bundle) as tar:
        return {m.name: tar.extractfile(m).read() for m in tar if m.isfile() or m.islnk()}


def test_stage_equals_bundle_unpacked(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    snap = bundle_network(net, cfg, md, out_path=tmp_path / "b", from_ref=None)
    s = stage_network(net, cfg, md, out_dir=tmp_path / "s", from_ref=None, to_ref=snap.created)
    assert s.out_path.is_dir() and s.out_path.parent == tmp_path / "s"
    staged = _tree(s.out_path)
    bundled = _members(snap.out_path)
    assert staged.keys() == bundled.keys()
    assert {k: v for k, v in staged.items() if k != ".mmw/manifest.jsonl"} == \
           {k: v for k, v in bundled.items() if k != ".mmw/manifest.jsonl"}
    payload = [p for p in s.out_path.rglob("*.deb")]
    assert payload and all(os.stat(p).st_nlink > 1 for p in payload)            # hardlinked from the pool
    with Pool(md) as p:
        last = p.bundles(net.name)[0]
    assert (last.kind, last.out_path) == ("stage", str(s.out_path))
    assert not (tmp_path / "s" / (s.out_path.name + ".tmp")).exists()
    assert not list((tmp_path / "s").glob("*.sha256"))


def test_stage_cross_filesystem_warns_and_copies(two_mirrors, tmp_path, monkeypatch):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    from mmw.core.snapshots import take_snapshot
    snap = take_snapshot(net, cfg, md)                  # pooling the scripts links too: do it first

    def no_link(src, dst):
        raise OSError(18, "Invalid cross-device link")
    monkeypatch.setattr(disk_io.ops, "link_into", no_link)
    monkeypatch.setattr(bundle_mod, "_same_filesystem", lambda a, b: False)
    s = stage_network(net, cfg, md, out_dir=tmp_path / "s", from_ref=None, to_ref=snap.stamp)
    assert any("another filesystem" in w and "copied, not hardlinked" in w for w in s.warnings)
    payload = list(s.out_path.rglob("*.deb"))
    assert payload and all(os.stat(p).st_nlink == 1 for p in payload)           # copies
    with Pool(md) as p:
        files = dict(p.snapshot_files(snap.id))
    import hashlib
    for path in payload:
        rel = path.relative_to(s.out_path).as_posix()
        assert hashlib.sha256(path.read_bytes()).hexdigest() == files[rel]


def test_stage_refused_on_apply_network(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = two_mirrors
    with pytest.raises(ValueError, match="mmw stage"):
        stage_network(net, cfg, md, out_dir=tmp_path)


def test_killed_stage_leaves_no_ledger_and_is_cleaned(two_mirrors, tmp_path, monkeypatch):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    from mmw.core.publisher import members
    real = members.DirMembers.add_blob

    def boom(self, *a, **kw):
        raise KeyboardInterrupt
    monkeypatch.setattr(members.DirMembers, "add_blob", boom)
    with pytest.raises(KeyboardInterrupt):
        stage_network(net, cfg, md, out_dir=tmp_path / "s", from_ref=None)
    with Pool(md) as p:
        assert p.bundles(net.name) == []
    assert list((tmp_path / "s").iterdir()) == []                              # aborted: nothing left
    monkeypatch.setattr(members.DirMembers, "add_blob", real)
    s = stage_network(net, cfg, md, out_dir=tmp_path / "s", from_ref=None)
    assert s.out_path.is_dir() and [p.name for p in (tmp_path / "s").iterdir()] == [s.out_path.name]


def test_tar_of_stage_dir_any_order_is_the_bundle(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    s = stage_network(net, cfg, md, out_dir=tmp_path / "s", from_ref=None)
    files = sorted((p for p in s.out_path.rglob("*") if p.is_file()), reverse=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for p in files:
            tar.add(p, arcname=p.relative_to(s.out_path).as_posix())
    out = tmp_path / "manual.tar"
    out.write_bytes(buf.getvalue())
    assert {m["name"] for m in describe(out)} == set(_tree(s.out_path))
    assert _members(out) == _tree(s.out_path)


def test_cli_stage(two_mirrors, tmp_path, monkeypatch):  # noqa: F811
    from typer.testing import CliRunner
    from mmw import cli
    from mmw.core import config
    cfg, net, md = _mmw(two_mirrors)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    config.save(cfg, cfg_dir)
    monkeypatch.setattr(cli, "_CFG_DIR", cli._CFG_DIR)
    run = lambda *a: CliRunner().invoke(cli.app, ["--config", str(cfg_dir), "stage", *a])  # noqa: E731
    r = run("-n", net.name, "--out", str(tmp_path / "s"), "--allow-incomplete")
    assert r.exit_code == 0, r.output
    (staged,) = (tmp_path / "s").iterdir()
    assert staged.is_dir() and str(staged) in r.output and "  a  full → v1" in r.output
    assert run("--out", str(tmp_path / "s")).exit_code != 0                     # -n is required
