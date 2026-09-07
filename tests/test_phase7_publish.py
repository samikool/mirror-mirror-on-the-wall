"""Publish-quality behaviors: by-hash lifecycle, read-only pool, nested-base
prune protection — all against the DB-driven publish."""
import hashlib
import os
import time

import pytest

from mmw.core import config
from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool
from mmw.core.publisher import TreeSink, publish_base, publish_group, publish_network


@pytest.fixture
def mirror_dir(tmp_path):
    return tmp_path / "mirror"


@pytest.fixture
def pool(mirror_dir):
    p = Pool(mirror_dir)
    yield p
    p.close()


def _seed(pool: Pool, content: bytes, name: str) -> str:
    checksum = hashlib.sha256(content).hexdigest()
    pool.add(content, checksum, "sha256", name, len(content))
    return checksum


def _apt_flat_mirror(name="m", url="https://example.com/repo/") -> config.MirrorConfig:
    return config.MirrorConfig(name=name, provider="apt-flat", label="custom", source_url=url)


def _seed_flat_index(pool: Pool, name: str, generation: int) -> str:
    """Reconcile mirror `name` to a fresh index generation. Returns its checksum."""
    content = f"index-v{generation}".encode()
    cs = _seed(pool, content, "Packages")
    rel = _seed(pool, f"release-v{generation}".encode(), "InRelease")
    pool.reconcile_refs(name, [
        (cs, "Packages", KIND_META),
        (rel, "InRelease", KIND_RELEASE),
    ])
    return cs


def test_by_hash_created_from_pool_key(tmp_path, pool):
    m = _apt_flat_mirror()
    cs = _seed_flat_index(pool, "m", 1)
    net = config.NetworkConfig(name="n", root=tmp_path / "pub")

    publish_group([m], TreeSink(net.root), pool)
    dst = publish_base(net, m)
    link = dst / "by-hash" / "SHA256" / cs
    assert link.exists()
    assert link.stat().st_ino == (dst / "Packages").stat().st_ino

    # idempotent
    result2 = publish_group([m], TreeSink(net.root), pool)
    assert link.exists()
    assert result2.errors == []


def test_by_hash_prunes_old_generations(tmp_path, pool):
    m = _apt_flat_mirror()
    net = config.NetworkConfig(name="n", root=tmp_path / "pub")
    hashes = []
    for gen in range(6):
        hashes.append(_seed_flat_index(pool, "m", gen))
        publish_group([m], TreeSink(net.root), pool)
        # stagger mtimes so retention ordering is deterministic
        bh = publish_base(net, m) / "by-hash" / "SHA256"
        t = time.time() - (6 - gen) * 10
        for e in bh.iterdir():
            if e.name != hashes[-1]:
                os.utime(e, (t, t))

    entries = {e.name for e in (publish_base(net, m) / "by-hash" / "SHA256").iterdir()}
    assert hashes[-1] in entries          # current generation always present
    # 2 live (Packages + InRelease) + keep-stale window
    assert len(entries) <= 2 + 2
    assert hashes[0] not in entries       # oldest gone


def test_read_only_pool_creates_nothing(tmp_path):
    mirror_dir = tmp_path / "mirror"
    mirror_dir.mkdir()
    with Pool(mirror_dir, read_only=True) as pool:
        assert pool.batch_contains(["abc"]) == set()
        assert pool.stats() == {"packages": 0, "total_size": 0}
        assert pool.mirror_stats("x") == (0, 0)
        assert pool.group_unique_count(["x"]) == 0
        assert pool.mirror_files(["x"]) == []
    assert not (mirror_dir / ".pool").exists()


def test_read_only_pool_reads_existing(tmp_path):
    mirror_dir = tmp_path / "mirror"
    with Pool(mirror_dir) as rw:
        cs = _seed(rw, b"bytes", "a.deb")
        rw.reconcile_refs("m", [(cs, "a.deb", KIND_PKG)])
    with Pool(mirror_dir, read_only=True) as ro:
        assert ro.contains(cs)
        assert ro.mirror_stats("m") == (1, 5)
        assert [r[0] for r in ro.mirror_files(["m"])] == ["a.deb"]


def _seed_flat(pool, name, url, fname):
    m = config.MirrorConfig(name=name, provider="apt-flat", label="custom", source_url=url)
    pkg = _seed(pool, f"pkg-{name}".encode(), fname)
    rel = _seed(pool, f"repomd-{name}".encode(), "repomd.xml")
    pool.reconcile_refs(name, [
        (pkg, fname, KIND_PKG),
        (rel, "repodata/repomd.xml", KIND_RELEASE),
    ])
    return m


def test_nested_publish_base_not_pruned(tmp_path, pool, mirror_dir):
    """Outer group's prune pass must not delete an inner group's published files."""
    outer = _seed_flat(pool, "outer", "https://example.com/repo/", "outer.deb")
    inner = _seed_flat(pool, "inner", "https://example.com/repo/sub/", "inner.deb")

    net = config.NetworkConfig(name="n", root=tmp_path / "pub", prune=True)
    cfg = config.Config(
        settings=config.Settings(mirror_dir=mirror_dir),
        sources=[
            config.SourceConfig(name="outer", label="custom", provider="apt-flat",
                                url="https://example.com/repo/"),
            config.SourceConfig(name="inner", label="custom", provider="apt-flat",
                                url="https://example.com/repo/sub/"),
        ],
        networks=[net],
    )

    result = publish_network(net, cfg, mirror_dir)
    assert result.errors == []
    inner_dst = publish_base(net, inner)
    assert (inner_dst / "inner.deb").exists()

    # second publish: outer group prunes; inner files must survive
    result2 = publish_network(net, cfg, mirror_dir)
    assert result2.errors == []
    assert (inner_dst / "inner.deb").exists()
    assert (inner_dst / "repodata" / "repomd.xml").exists()
    assert (publish_base(net, outer) / "outer.deb").exists()
    assert sum(r.pruned for r in result2.mirrors) == 0


def test_publish_network_without_root_returns_error(tmp_path, mirror_dir):
    """A bundle-only network (no publish root) must not attempt to build a
    TreeSink — publish_base would raise ValueError, and `mmw bundle` owns
    this case instead. Nothing in the pool/filesystem should be touched."""
    net = config.NetworkConfig(name="dgx-site")  # root=None, bundle_dir=None
    cfg = config.Config(
        settings=config.Settings(mirror_dir=mirror_dir),
        sources=[],
        networks=[net],
    )

    result = publish_network(net, cfg, mirror_dir)

    assert len(result.mirrors) == 1
    only = result.mirrors[0]
    assert only.mirror_name == "dgx-site"
    assert only.errors == [
        "network 'dgx-site' has no publish root — use `mmw bundle` instead"
    ]
    assert only.linked == 0
    assert only.skipped == 0
    assert only.pruned == 0
    # The pool was never opened — an early return, not a failed operation.
    assert not (mirror_dir / ".pool").exists()


def test_publish_writes_nginx_site_files(tmp_path, pool, mirror_dir):
    """A real publish generates nginx/mirror.conf + one site file per host, with
    the network's own root substituted — no placeholder left behind."""
    _seed_flat(pool, "m", "https://example.com/repo/", "pkg.deb")
    net = config.NetworkConfig(name="n", root=tmp_path / "pub")
    cfg = config.Config(
        settings=config.Settings(mirror_dir=mirror_dir),
        sources=[config.SourceConfig(name="m", label="custom", provider="apt-flat",
                                     url="https://example.com/repo/")],
        networks=[net],
    )

    result = publish_network(net, cfg, mirror_dir)
    assert result.errors == []

    site = net.root / "nginx" / "sites" / "example.com.conf"
    assert site.exists()
    content = site.read_text()
    assert f"root {net.root}/example.com;" in content
    assert "__MMW_ROOT__" not in content
    assert (net.root / "nginx" / "mirror.conf").exists()


def test_publish_nginx_false_writes_no_site_files(tmp_path, pool, mirror_dir):
    _seed_flat(pool, "m", "https://example.com/repo/", "pkg.deb")
    net = config.NetworkConfig(name="n", root=tmp_path / "pub", nginx=False)
    cfg = config.Config(
        settings=config.Settings(mirror_dir=mirror_dir),
        sources=[config.SourceConfig(name="m", label="custom", provider="apt-flat",
                                     url="https://example.com/repo/")],
        networks=[net],
    )

    result = publish_network(net, cfg, mirror_dir)
    assert result.errors == []
    assert not (net.root / "nginx").exists()


def test_publish_group_prune_exclude_direct(tmp_path, pool):
    m = _apt_flat_mirror()
    _seed_flat_index(pool, "m", 1)
    net = config.NetworkConfig(name="n", root=tmp_path / "pub")
    dst = publish_base(net, m)
    protected = dst / "sub"
    protected.mkdir(parents=True)
    (protected / "keep.deb").write_bytes(b"keep")

    result = publish_group([m], TreeSink(net.root), pool, prune=True,
                           prune_exclude={f"{m.mirror_base}/sub"})
    assert (protected / "keep.deb").exists()
    assert result.pruned == 0


def test_publish_group_stop_between_chunks(tmp_path, pool, monkeypatch):
    import threading

    from mmw.core.publisher import links as links_mod
    monkeypatch.setattr(links_mod, "_LINK_CHUNK", 2)

    entries = []
    for i in range(10):
        cs = _seed(pool, f"pkg{i}".encode(), f"p{i}.deb")
        entries.append((cs, f"Packages/p{i}.deb", KIND_PKG))
    pool.reconcile_refs("m", entries)
    m = config.MirrorConfig(name="m", provider="apt-flat", label="custom",
                            source_url="https://example.com/repo/")
    net = config.NetworkConfig(name="n", root=tmp_path / "pub")

    stop = threading.Event()
    seen = []

    def progress(dst, status):
        # Only link events count — enumeration/scan events bracket the phase.
        if not status.startswith(("linked", "skipped")):
            return
        seen.append(status)
        if len(seen) == 2:
            stop.set()

    result = publish_group([m], TreeSink(net.root, stop_event=stop), pool,
                           progress=progress, stop_event=stop)
    assert result.linked < 10  # interrupted between chunks
    assert result.linked >= 2


# ── Removing mirrors from a network must not delete kept mirrors' files ──────

def _suite_mirror(name: str, suite: str, url: str) -> config.MirrorConfig:
    return config.MirrorConfig(name=name, provider="apt", label="ubuntu", source_url=url,
                               suite=suite, component="main", arch="amd64")


def test_removed_mirror_sharing_a_base_is_never_deleted(tmp_path):
    """Dropping jammy must not offer to delete the tree noble still publishes to."""
    from mmw.tui.publish_panel import exclusive_publish_dirs

    archive = "http://archive.ubuntu.com/ubuntu"
    jammy = _suite_mirror("u-jammy-main-amd64", "jammy", archive)
    noble = _suite_mirror("u-noble-main-amd64", "noble", archive)
    mirror_map = {m.name: m for m in (jammy, noble)}
    net = config.NetworkConfig(name="lab", root=tmp_path / "pub")
    (net.root / "archive.ubuntu.com" / "ubuntu").mkdir(parents=True)

    dirs, shared = exclusive_publish_dirs(net, mirror_map, {jammy.name}, {noble.name})
    assert dirs == []                                # nothing safe to delete
    assert shared == ["archive.ubuntu.com/ubuntu"]   # caller warns instead


def test_removed_mirror_with_its_own_base_is_deletable(tmp_path):
    """A base no kept mirror publishes to is still offered for deletion."""
    from mmw.tui.publish_panel import exclusive_publish_dirs

    archive = _suite_mirror("u-noble-main-amd64", "noble", "http://archive.ubuntu.com/ubuntu")
    ports = config.MirrorConfig(name="u-ports-noble-main-arm64", provider="apt", label="ubuntu",
                                source_url="http://ports.ubuntu.com/ubuntu-ports",
                                suite="noble", component="main", arch="arm64")
    mirror_map = {m.name: m for m in (archive, ports)}
    net = config.NetworkConfig(name="lab", root=tmp_path / "pub")
    (net.root / "ports.ubuntu.com" / "ubuntu-ports").mkdir(parents=True)
    (net.root / "archive.ubuntu.com" / "ubuntu").mkdir(parents=True)

    dirs, shared = exclusive_publish_dirs(net, mirror_map, {ports.name}, {archive.name})
    assert dirs == [net.root / "ports.ubuntu.com" / "ubuntu-ports"]
    assert shared == []


def test_removed_mirror_base_that_was_never_published_is_skipped(tmp_path):
    from mmw.tui.publish_panel import exclusive_publish_dirs

    ports = config.MirrorConfig(name="u-ports-noble-main-arm64", provider="apt", label="ubuntu",
                                source_url="http://ports.ubuntu.com/ubuntu-ports",
                                suite="noble", component="main", arch="arm64")
    net = config.NetworkConfig(name="lab", root=tmp_path / "pub")

    dirs, shared = exclusive_publish_dirs(net, {ports.name: ports}, {ports.name}, set())
    assert dirs == []  # nothing on disk
    assert shared == []
