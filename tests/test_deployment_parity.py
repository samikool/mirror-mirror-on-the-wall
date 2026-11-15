"""A published tree and the tree `mmw site apply` lays out from the same
network's bundle serve the same files — everything but `.mmw/`, where each
side keeps its own records and TLS keys (the far side makes its own)."""
# ruff: noqa: F811
import stat

import pytest

from mmw.core.config import NetworkConfig
from mmw.core.pool import Pool
from mmw.core.publisher import publish_network, publish_snapshot
from mmw.core.publisher.bundle import bundle_network
from mmw.site import home as H
from mmw.site.apply import apply_bundle
from test_bundle import seeded, _sign_lab_cert  # noqa: F401


def _files(root):
    """What a tree serves: every file outside `.mmw/` (and an old state file)."""
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and not p.relative_to(root).as_posix().startswith(".mmw/")
            and p.name != ".mmw-state"}


def _apply(net, bundle, root):
    home = H.init_root(root, name="main", set_id="s1", network=net.name)
    apply_bundle(home, str(bundle), say=lambda *a: None)
    return root


@pytest.mark.parametrize("tls", ["off", "unsigned", "signed"])
@pytest.mark.parametrize("nginx", [False, True])
def test_published_and_applied_trees_are_one_to_one(seeded, tmp_path, tls, nginx):
    mirror_dir, cfg = seeded
    if tls == "signed":
        _sign_lab_cert(tmp_path, mirror_dir, ["archive.ubuntu.com"])
    net = NetworkConfig(name="lab", root=tmp_path / "published", bundle_dir=tmp_path / "bundles",
                        certs=tls != "off", nginx=nginx)
    published = publish_network(net, cfg, mirror_dir)
    assert not published.errors
    with Pool(mirror_dir) as pool:
        assert pool.snapshots(net.name) == [], "publish never takes a snapshot"
    bundled = bundle_network(net, cfg, mirror_dir, from_ref=None)
    applied = _apply(net, bundled.out_path, tmp_path / "applied")
    assert _files(net.root) == _files(applied)
    with Pool(mirror_dir) as pool:
        assert pool.snapshots(net.name) == [], "nor does bundling"
    # Snapshot replay lays out the same tree again.
    from mmw.core.snapshots import take_snapshot
    snap = take_snapshot(net, cfg, mirror_dir, label="parity")
    restored = tmp_path / "snapshot-publish"
    assert not publish_snapshot(net, cfg, mirror_dir, snap.stamp, root=restored).errors
    assert _files(restored) == _files(applied)
    assert not any((net.root / n).exists() for n in ("apply.py", "sign.py", "make-ca.py"))
    if tls != "off":
        assert stat.S_IMODE((net.root / ".mmw/tls").stat().st_mode) == 0o700
        assert stat.S_IMODE((net.root / ".mmw/tls/mirror.key").stat().st_mode) == 0o600
        assert (net.root / ".mmw/tls/https.conf").exists() == (tls == "signed")
        assert not (applied / ".mmw/tls/mirror.key").exists()        # the far side makes its own


def test_snapshot_outputs_stay_equal_after_network_flags_change(seeded, tmp_path):
    from mmw.core.snapshots import take_snapshot

    mirror_dir, cfg = seeded
    net = NetworkConfig(name="lab", root=tmp_path / "published", certs=True, nginx=True)
    _sign_lab_cert(tmp_path, mirror_dir, ["archive.ubuntu.com"])
    snapshot = take_snapshot(net, cfg, mirror_dir)
    changed = net.model_copy(update={"certs": False, "nginx": False})
    published = publish_snapshot(changed, cfg, mirror_dir, snapshot.stamp)
    assert not published.errors
    bundle = bundle_network(changed, cfg, mirror_dir, to_ref=snapshot.stamp, from_ref=None,
                            out_path=tmp_path / "old-snapshot.tar")
    applied = _apply(changed, bundle.out_path, tmp_path / "applied-old")
    assert _files(net.root) == _files(applied)
