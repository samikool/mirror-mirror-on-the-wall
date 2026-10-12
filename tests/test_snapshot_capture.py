"""Taking a snapshot = the tree a publish would lay out, recorded instead of
written: same paths, same checksums. Keys and upstream files are frozen into
the pool (a later sync rewriting them can't change the snapshot); the nginx
config is left out (regenerated per destination)."""
import hashlib
import json
from pathlib import Path

from mmw.core.pool import Pool
from mmw.core.publisher import publish_network
from mmw.core.snapshots import take_snapshot

from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture


def _tree(root: Path) -> dict[str, str]:
    out = {}
    for p in root.rglob("*"):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and not rel.startswith(("nginx/", ".mmw")):
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _key(mirror_dir: Path, body: bytes = b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nk1\n") -> Path:
    key = mirror_dir / "keys" / "custom" / "vendor.asc"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_bytes(body)
    return key


def test_a_snapshot_is_exactly_the_published_tree(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    _key(mirror_dir)
    assert not publish_network(net, cfg, mirror_dir).errors
    published = _tree(net.root)
    assert "keys/custom/vendor.asc" in published

    snap = take_snapshot(net, cfg, mirror_dir, label="first")
    with Pool(mirror_dir, read_only=True) as pool:
        rows = dict(pool.snapshot_files(snap.id))
    assert rows == published
    assert (snap.label, snap.kind, snap.complete, snap.files) == ("first", "manual", True,
                                                                   len(published))
    settings = json.loads(snap.settings)
    assert settings["network"]["name"] == "oak"
    assert sorted(m["name"] for m in settings["mirrors"]) == ["a", "b"]


def test_keys_are_frozen_into_the_pool_and_never_moved(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    key = _key(mirror_dir, b"key version one\n")
    snap = take_snapshot(net, cfg, mirror_dir)
    assert key.read_bytes() == b"key version one\n", "the mirror's own key file was moved"

    key.write_bytes(b"key version two\n")          # a later sync re-fetched it
    with Pool(mirror_dir) as pool:
        rows = dict(pool.snapshot_files(snap.id))
        frozen = rows["keys/custom/vendor.asc"]
        assert frozen == hashlib.sha256(b"key version one\n").hexdigest()
        assert pool.contains(frozen)
        pool.prune_orphans([s.url for s in cfg.sources])
        assert pool.contains(frozen), "pruned although a snapshot lists it"


def test_a_bundle_only_network_can_be_snapshotted(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    bundle_only = net.model_copy(update={"root": None, "bundle_dir": mirror_dir.parent / "usb"})
    snap = take_snapshot(bundle_only, cfg, mirror_dir, kind="bundle")
    assert snap.files > 0 and snap.kind == "bundle"


def test_two_snapshots_in_the_same_second_get_distinct_ids(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    a = take_snapshot(net, cfg, mirror_dir)
    b = take_snapshot(net, cfg, mirror_dir)
    assert a.stamp != b.stamp


def test_taking_a_snapshot_holds_the_pool_against_prune(tmp_path, monkeypatch):
    """Frozen keys are pool rows with no ref and no snapshot row until the
    snapshot commits — orphans, to a prune running in that window."""
    import threading

    from mmw.core import config as config_mod
    from mmw.core import publisher
    from mmw.core.pool import pool_is_busy
    from mmw.core.snapshots import take_snapshot

    mirror_dir = tmp_path / "mirror"
    cfg = config_mod.Config(settings=config_mod.Settings(mirror_dir=mirror_dir))
    net = config_mod.NetworkConfig(name="lab")
    walking, release = threading.Event(), threading.Event()

    def slow_walk(*a, **k):
        walking.set()
        assert release.wait(10)
        return []
    monkeypatch.setattr(publisher, "walk_network", slow_walk)

    t = threading.Thread(target=take_snapshot, args=(net, cfg, mirror_dir))
    t.start()
    assert walking.wait(10)
    try:
        assert pool_is_busy(mirror_dir), "a prune could run while the snapshot is being taken"
    finally:
        release.set()
        t.join(10)
    assert not pool_is_busy(mirror_dir)
