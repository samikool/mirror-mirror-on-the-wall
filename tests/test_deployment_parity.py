"""Copied publish trees and applied bundles carry the same deployment assets."""
# ruff: noqa: F811
import shutil
import stat
import subprocess
import sys

import pytest

from mmw.core.config import NetworkConfig
from mmw.core.pool import Pool
from mmw.core.publisher import publish_network, publish_snapshot
from mmw.core.publisher.bundle import bundle_network
from mmw.resources import apply_script_bytes, ca_script_bytes, sign_script_bytes
from test_bundle import seeded, _sign_lab_cert  # noqa: F401


def _files(root):
    # The bundle cursor is snapshot metadata; publish snapshots are manual.
    # The record's header says who wrote it and when; its rows are compared
    # on their own (`_record_rows`).
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and p.relative_to(root).as_posix()
            not in (".mmw-state", ".mmw/manifest.jsonl")}


def _record_rows(root):
    from mmw.core.publisher.manifest import RECORD_PATH, iter_manifest
    _header, rows = iter_manifest(root / RECORD_PATH)
    return dict(rows)


@pytest.mark.parametrize("tls", ["off", "unsigned", "signed"])
@pytest.mark.parametrize("nginx", [False, True])
def test_copied_publish_and_applied_bundle_are_one_to_one(seeded, tmp_path, tls, nginx):
    mirror_dir, cfg = seeded
    if tls == "signed":
        _sign_lab_cert(tmp_path, mirror_dir, ["archive.ubuntu.com"])
    net = NetworkConfig(name="lab", root=tmp_path / "published", bundle_dir=tmp_path / "bundles",
                        certs=tls != "off", nginx=nginx)
    published = publish_network(net, cfg, mirror_dir)
    assert not published.errors
    with Pool(mirror_dir) as pool:
        assert pool.snapshots(net.name) == [], "publish must never create an automatic snapshot"
    copied = tmp_path / "drive-copy"
    shutil.copytree(net.root, copied)
    assert (copied / "apply.py").read_bytes() == apply_script_bytes()

    bundled = bundle_network(net, cfg, mirror_dir, full=True)
    assert not bundled.errors
    applied = tmp_path / "applied"
    run = subprocess.run([sys.executable, str(bundled.out_path.parent / "apply.py"), str(bundled.out_path),
                          "--root", str(applied), "--verify"], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert _files(copied) == _files(applied)
    assert _record_rows(copied) == _record_rows(applied), \
        "a published tree and an applied bundle must carry the same record"

    restored = tmp_path / "snapshot-publish"
    replay = publish_snapshot(net, cfg, mirror_dir, bundled.created, root=restored)
    assert not replay.errors
    assert _files(restored) == _files(applied)
    with Pool(mirror_dir) as pool:
        assert len(pool.snapshots(net.name)) == 1, "only bundling creates the automatic snapshot"
    # No pool or original publication is needed to run the copied tools.
    status = subprocess.run([sys.executable, str(copied / "apply.py"), "--status", "--root", str(copied)],
                            capture_output=True, text=True)
    assert status.returncode == 0
    if tls != "off":
        assert (copied / "sign.py").read_bytes() == sign_script_bytes()
        assert (copied / "make-ca.py").read_bytes() == ca_script_bytes()
        assert stat.S_IMODE((copied / ".mmw/tls").stat().st_mode) == 0o700
        assert stat.S_IMODE((copied / ".mmw/tls/mirror.key").stat().st_mode) == 0o600
        state = subprocess.run([sys.executable, str(copied / "apply.py"), "--tls", "--root", str(copied)],
                               capture_output=True, text=True)
        assert state.returncode == 0, state.stderr
        assert (copied / ".mmw/tls/https.conf").exists() == (tls == "signed")


def test_snapshot_outputs_stay_equal_after_network_flags_change(seeded, tmp_path):
    from mmw.core.snapshots import take_snapshot

    mirror_dir, cfg = seeded
    net = NetworkConfig(name="lab", root=tmp_path / "published", certs=True, nginx=True)
    _sign_lab_cert(tmp_path, mirror_dir, ["archive.ubuntu.com"])
    snapshot = take_snapshot(net, cfg, mirror_dir)
    changed = net.model_copy(update={"certs": False, "nginx": False})
    published = publish_snapshot(changed, cfg, mirror_dir, snapshot.stamp)
    assert not published.errors
    bundle = bundle_network(changed, cfg, mirror_dir, to_ref=snapshot.stamp, full=True,
                            out_path=tmp_path / "old-snapshot.tar")
    applied = tmp_path / "applied-old"
    run = subprocess.run([sys.executable, str(bundle.out_path.parent / "apply.py"), str(bundle.out_path),
                          "--root", str(applied), "--verify"], capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    assert _files(net.root) == _files(applied)
