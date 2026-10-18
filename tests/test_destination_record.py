"""Publish trusts the destination's record — the same manifest a bundle keeps.

`{root}/.mmw/manifest.jsonl` says what a publish last laid out. The next
publish skips every path whose checksum is unchanged without touching the
filesystem, and prunes from the record instead of walking the tree. `--recheck`
ignores the record and rechecks everything.
"""
# ruff: noqa: F811
import hashlib
import json
import os
from pathlib import Path

import pytest

from mmw.core.config import NetworkConfig
from mmw.core.pool import KIND_PKG, Pool
from mmw.core.publisher import links, publish_network, publish_snapshot
from mmw.core.publisher.manifest import RECORD_PATH, iter_manifest
from mmw.core.snapshots import take_snapshot
from test_bundle import seeded  # noqa: F401

MIRROR = "ubuntu-noble-main-amd64"
SRC = "https://archive.ubuntu.com/ubuntu"
DEB = "archive.ubuntu.com/ubuntu/pool/main/h/hello/hello_1.0_amd64.deb"


def _net(tmp_path, **kw):
    return NetworkConfig(name="lab", root=tmp_path / "pub", nginx=False, **kw)


def _record(root: Path) -> dict[str, str]:
    _header, rows = iter_manifest(root / RECORD_PATH)
    return dict(rows)


def _set_refs(mirror_dir, extra: list[tuple[str, bytes]] = (), drop: str | None = None):
    """Replace the mirror's refs: keep the seeded ones (minus `drop`), add `extra`."""
    with Pool(mirror_dir, defer_writes=False) as pool:
        refs = [(cs, p, k) for p, _pp, _s, k, cs in pool.mirror_files([MIRROR]) if p != drop]
        for repo_path, content in extra:
            cs = hashlib.sha256(content).hexdigest()
            pool.add(content, cs, "sha256", Path(repo_path).name, len(content))
            refs = [r for r in refs if r[1] != repo_path] + [(cs, repo_path, KIND_PKG)]
        pool.reconcile_refs(MIRROR, refs, source=SRC)


@pytest.fixture
def counted(monkeypatch):
    calls = {"link": 0, "same": 0, "walk": 0}
    real_link, real_same, real_walk = links.hardlink_or_copy, links.same_file, os.walk

    def link(*a, **kw):
        calls["link"] += 1
        return real_link(*a, **kw)

    def same(*a, **kw):
        calls["same"] += 1
        return real_same(*a, **kw)

    def walk(*a, **kw):
        calls["walk"] += 1
        return real_walk(*a, **kw)
    monkeypatch.setattr(links, "hardlink_or_copy", link)
    monkeypatch.setattr(links, "same_file", same)
    monkeypatch.setattr("mmw.core.publisher.sinks.os.walk", walk)
    return calls


def test_a_publish_records_what_it_laid_out(seeded, tmp_path):
    mirror_dir, cfg = seeded
    net = _net(tmp_path)
    assert not publish_network(net, cfg, mirror_dir).errors
    header = json.loads((net.root / RECORD_PATH).read_text().splitlines()[0])
    assert header["network"] == "lab"
    assert DEB in _record(net.root)


def test_an_unchanged_republish_touches_no_file(seeded, tmp_path, counted):
    mirror_dir, cfg = seeded
    net = _net(tmp_path)
    publish_network(net, cfg, mirror_dir)
    before = _record(net.root)
    for k in counted:
        counted[k] = 0
    result = publish_network(net, cfg, mirror_dir)
    assert not result.errors
    assert counted == {"link": 0, "same": 0, "walk": 0}
    assert _record(net.root) == before


def test_one_changed_file_is_the_only_relink(seeded, tmp_path, counted):
    mirror_dir, cfg = seeded
    net = _net(tmp_path)
    publish_network(net, cfg, mirror_dir)
    _set_refs(mirror_dir, extra=[("pool/main/h/hello/hello_1.0_amd64.deb", b"hello v2")])
    counted["link"] = 0
    publish_network(net, cfg, mirror_dir)
    assert counted["link"] == 1
    assert (net.root / DEB).read_bytes() == b"hello v2"


def test_a_dropped_file_is_pruned_from_the_record_without_a_walk(seeded, tmp_path, counted):
    mirror_dir, cfg = seeded
    net = _net(tmp_path)
    _set_refs(mirror_dir, extra=[("pool/main/x/extra.deb", b"extra")])
    publish_network(net, cfg, mirror_dir)
    extra = net.root / "archive.ubuntu.com/ubuntu/pool/main/x/extra.deb"
    assert extra.exists()
    _set_refs(mirror_dir, drop="pool/main/x/extra.deb")
    counted["walk"] = 0
    publish_network(net, cfg, mirror_dir)
    assert not extra.exists() and not extra.parent.exists()
    assert counted["walk"] == 0


def test_a_publish_stopped_midway_leaves_no_record(seeded, tmp_path):
    import threading
    mirror_dir, cfg = seeded
    net = _net(tmp_path)
    publish_network(net, cfg, mirror_dir)
    stop = threading.Event()

    def progress(rel, status):
        if status in ("linked", "skipped"):
            stop.set()
    publish_network(net, cfg, mirror_dir, progress=progress, stop_event=stop, recheck=True)
    assert stop.is_set()
    assert not (net.root / RECORD_PATH).exists()


def test_a_stop_before_anything_is_written_keeps_the_record(seeded, tmp_path):
    import threading
    mirror_dir, cfg = seeded
    net = _net(tmp_path)
    publish_network(net, cfg, mirror_dir)
    before = _record(net.root)
    stop = threading.Event()
    stop.set()                                    # e.g. pressed while warming
    publish_network(net, cfg, mirror_dir, stop_event=stop)
    assert _record(net.root) == before


def test_a_database_that_will_not_open_keeps_the_record(seeded, tmp_path):
    from mmw.core.pool import PoolDatabaseMissing, register_database
    mirror_dir, cfg = seeded
    net = _net(tmp_path)
    publish_network(net, cfg, mirror_dir)
    before = _record(net.root)
    register_database(mirror_dir, tmp_path / "nowhere" / "mmw.db")
    try:
        with pytest.raises(PoolDatabaseMissing):
            publish_network(net, cfg, mirror_dir)
    finally:
        register_database(mirror_dir, mirror_dir / ".pool" / "mmw.db")
    assert _record(net.root) == before


def test_a_publish_with_errors_leaves_no_record(seeded, tmp_path, monkeypatch):
    mirror_dir, cfg = seeded
    net = _net(tmp_path)

    def fail(src, dst, tmp_dir=None):
        raise OSError("disk full")
    monkeypatch.setattr(links, "hardlink_or_copy", fail)
    result = publish_network(net, cfg, mirror_dir)
    assert result.errors
    assert not (net.root / RECORD_PATH).exists()
    # Said, not silent: a unit that always fails would otherwise make every
    # publish a full check with no hint why.
    assert any("record" in w and "every file" in w for w in result.warnings)


def test_another_networks_record_is_ignored(seeded, tmp_path, counted):
    mirror_dir, cfg = seeded
    publish_network(_net(tmp_path), cfg, mirror_dir)
    record = net_root = tmp_path / "pub"
    path = record / RECORD_PATH
    lines = path.read_text().splitlines()
    header = json.loads(lines[0])
    header["network"] = "other"
    path.write_text("\n".join([json.dumps(header), *lines[1:]]) + "\n")
    counted["same"] = 0
    publish_network(_net(tmp_path), cfg, mirror_dir)
    assert counted["same"] > 0          # checked the tree, did not trust the record
    assert json.loads((net_root / RECORD_PATH).read_text().splitlines()[0])["network"] == "lab"


def test_full_repairs_hand_edits_and_rewrites_the_record(seeded, tmp_path):
    mirror_dir, cfg = seeded
    net = _net(tmp_path)
    publish_network(net, cfg, mirror_dir)
    deb = net.root / DEB
    deb.unlink()
    release = net.root / "archive.ubuntu.com/ubuntu/dists/noble/InRelease"
    release.unlink()
    release.write_bytes(b"tampered")
    publish_network(net, cfg, mirror_dir)             # trusted: does not notice
    assert not deb.exists()
    assert not publish_network(net, cfg, mirror_dir, recheck=True).errors
    assert deb.read_bytes() == b"hello-pkg"
    assert release.read_bytes() == b"signed-release"
    assert DEB in _record(net.root)


def test_a_snapshot_publish_trusts_and_writes_the_record_too(seeded, tmp_path, counted):
    mirror_dir, cfg = seeded
    net = _net(tmp_path)
    stamp = take_snapshot(net, cfg, mirror_dir, label="s").stamp
    assert not publish_snapshot(net, cfg, mirror_dir, stamp).errors
    assert DEB in _record(net.root)
    counted["link"] = counted["same"] = 0
    publish_snapshot(net, cfg, mirror_dir, stamp)
    assert counted["link"] == 0 and counted["same"] == 0


@pytest.mark.parametrize("trusted", [False, True])
def test_an_outer_group_never_prunes_a_nested_bases_by_hash(tmp_path, trusted):
    """`a/b` is its own base inside `a`. Finalizing `a` with `exclude={"a/b"}`
    must leave `a/b`'s by-hash alone, record or walk."""
    from mmw.core.publisher.sinks import TreeSink
    root, blobs = tmp_path / "t", tmp_path / "blobs"
    blobs.mkdir()
    live = [f"{i:064x}" for i in range(3)]
    rels = {f"a/b/dists/x/by-hash/SHA256/{cs}": cs for cs in live}
    TreeSink(root).close()
    first = TreeSink(root)
    for rel, cs in rels.items():
        (blobs / cs).write_bytes(cs.encode())
        assert first.link(blobs / cs, rel, 64, cs, "meta") == "linked"
    sink = TreeSink(root, prev=dict(rels) if trusted else None)
    for rel, cs in rels.items():
        sink.link(blobs / cs, rel, 64, cs, "meta")
    sink.finalize_group("a/b", set(), set(live), set())
    sink.finalize_group("a", set(), set(), {"a/b"})
    bh = root / "a/b/dists/x/by-hash/SHA256"
    assert sorted(p.name for p in bh.iterdir()) == live


def test_trusted_finalize_scales_with_records_not_groups_times_records(tmp_path):
    """VS Code finalizes once per publisher host: tens of thousands of groups
    over a 1M-file record. Each group must look only at its own records."""
    import time

    from mmw.core.publisher.sinks import TreeSink
    groups, per = 5000, 20
    prev = {f"h{g}.example/f{i}": "c" for g in range(groups) for i in range(per)}
    sink = TreeSink(tmp_path / "t", prev=prev)
    started = time.perf_counter()
    for g in range(groups):
        keep = {f"h{g}.example/f{i}" for i in range(per)}
        sink.finalize_group(f"h{g}.example", keep, set(), set())
    assert time.perf_counter() - started < 5


def test_turning_prune_on_cleans_what_a_non_pruning_publish_left(seeded, tmp_path):
    mirror_dir, cfg = seeded
    _set_refs(mirror_dir, extra=[("pool/main/x/extra.deb", b"extra")])
    publish_network(_net(tmp_path, prune=False), cfg, mirror_dir)
    _set_refs(mirror_dir, drop="pool/main/x/extra.deb")
    publish_network(_net(tmp_path, prune=False), cfg, mirror_dir)
    extra = tmp_path / "pub/archive.ubuntu.com/ubuntu/pool/main/x/extra.deb"
    assert extra.exists()                          # prune off: left in place
    publish_network(_net(tmp_path), cfg, mirror_dir)
    assert not extra.exists()


def test_a_stale_file_that_cannot_be_removed_stays_in_the_record(tmp_path):
    """Pruned from the record, it would be invisible to every later trusted
    publish and served forever; kept, the next publish tries again."""
    from mmw.core.publisher.sinks import TreeSink
    root = tmp_path / "t"
    locked = root / "base/locked"
    locked.mkdir(parents=True)
    (locked / "stale.deb").write_bytes(b"old")
    locked.chmod(0o500)
    try:
        sink = TreeSink(root, prev={"base/locked/stale.deb": "c1"})
        sink.finalize_group("base", set(), set(), set())
        assert ("base/locked/stale.deb", "c1") in sink.manifest_rows
    finally:
        locked.chmod(0o700)
    sink = TreeSink(root, prev={"base/locked/stale.deb": "c1"})
    sink.finalize_group("base", set(), set(), set())
    assert not (locked / "stale.deb").exists()
    assert ("base/locked/stale.deb", "c1") not in sink.manifest_rows


def test_an_interrupted_apply_leaves_no_record(seeded, tmp_path):
    import subprocess
    import sys

    from mmw.core.publisher.bundle import bundle_network
    mirror_dir, cfg = seeded
    net = NetworkConfig(name="lab", bundle_dir=tmp_path / "out", nginx=False)
    first = bundle_network(net, cfg, mirror_dir)
    far = tmp_path / "far"
    apply = str(tmp_path / "out" / "apply.py")
    subprocess.run([sys.executable, apply, str(first.out_path), "--root", str(far)], check=True)
    assert (far / RECORD_PATH).exists()
    _set_refs(mirror_dir, extra=[("pool/main/h/hello/hello_1.0_amd64.deb", b"x" * 200_000)])
    second = bundle_network(net, cfg, mirror_dir)
    data = second.out_path.read_bytes()
    second.out_path.write_bytes(data[: len(data) // 2])          # cut mid-payload
    run = subprocess.run([sys.executable, apply, str(second.out_path), "--root", str(far)],
                         capture_output=True, text=True)
    assert run.returncode != 0
    assert not (far / RECORD_PATH).exists()


def test_a_corrupt_record_header_is_ignored_without_leaking_its_file(tmp_path):
    from mmw.core.publisher.manifest import read_record
    path = tmp_path / RECORD_PATH
    path.parent.mkdir(parents=True)
    path.write_text("not json\n{\"p\": \"x\", \"c\": \"y\"}\n")
    import gc
    import warnings
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        assert read_record(tmp_path, "lab") is None
        gc.collect()
    assert not [w for w in caught if issubclass(w.category, ResourceWarning)]
