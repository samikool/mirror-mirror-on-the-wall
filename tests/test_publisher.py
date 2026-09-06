import hashlib
from pathlib import Path

import pytest

from mmw.core import config
from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool
from mmw.core.publisher import (
    MirrorPublishResult,
    NetworkPublishResult,
    parse_meta_release_lts,
    publish_base,
    publish_group,
)


def _mirror(url: str, **kw) -> config.MirrorConfig:
    defaults = dict(name="m", provider="rpm", label="custom")
    defaults.update(kw)
    return config.MirrorConfig(source_url=url, **defaults)


def _network(root: Path, **kw) -> config.NetworkConfig:
    return config.NetworkConfig(name="net", root=root, **kw)


@pytest.fixture
def pool(tmp_path):
    p = Pool(tmp_path / "mirror")
    yield p
    p.close()


def _seed(pool: Pool, content: bytes, name: str) -> str:
    checksum = hashlib.sha256(content).hexdigest()
    pool.add(content, checksum, "sha256", name, len(content))
    return checksum


def test_publish_base(tmp_path):
    net = _network(tmp_path / "pub")
    m = _mirror("https://dl.rockylinux.org/pub/rocky/9.4/BaseOS/x86_64/os/")
    assert publish_base(net, m) == tmp_path / "pub" / "dl.rockylinux.org" / "pub/rocky/9.4/BaseOS/x86_64/os"


def test_result_aggregation():
    r = NetworkPublishResult(network_name="n", mirrors=[
        MirrorPublishResult(mirror_name="a", linked=2, linked_bytes=10, skipped=1, skipped_bytes=5),
        MirrorPublishResult(mirror_name="b", pruned=3, pruned_bytes=7, errors=["x"]),
    ], pool_pruned=1, pool_pruned_bytes=2)
    assert r.linked == 2
    assert r.skipped == 1
    assert r.pruned == 4
    assert r.pruned_bytes == 9
    assert r.total_files == 3
    assert r.total_bytes == 15
    assert r.errors == ["x"]


META_RELEASE_LTS = """\
Dist: focal
Name: Focal Fossa
Version: 20.04.6 LTS
Supported: 1

Dist: jammy
Name: Jammy Jellyfish
Version: 22.04 LTS
Supported: 1
"""


def test_parse_meta_release_lts(pool):
    cs = _seed(pool, META_RELEASE_LTS.encode(), "meta-release-lts")
    pool.reconcile_refs("ubuntu-jammy-main", [
        (cs, "changelogs.ubuntu.com/meta-release-lts", KIND_META),
    ])
    assert parse_meta_release_lts(pool) == [("focal", "20.04.6"), ("jammy", "22.04")]


def test_parse_meta_release_lts_missing(pool):
    assert parse_meta_release_lts(pool) == []


def _seed_rpm_mirror(pool: Pool, name="m") -> config.MirrorConfig:
    m = _mirror("https://example.com/repo/", name=name)
    pkg = _seed(pool, b"rpm-bytes", "bash.rpm")
    primary = _seed(pool, b"<primary/>", "primary.xml.gz")
    repomd = _seed(pool, b"<repomd/>", "repomd.xml")
    pool.reconcile_refs(name, [
        (pkg, "Packages/bash.rpm", KIND_PKG),
        (primary, "repodata/abc-primary.xml.gz", KIND_META),
        (repomd, "repodata/repomd.xml", KIND_RELEASE),
    ])
    return m


def test_publish_group_links_and_skips(tmp_path, pool):
    m = _seed_rpm_mirror(pool)
    net = _network(tmp_path / "pub")

    result = publish_group([m], net, pool)
    assert result.errors == []
    assert result.linked == 3
    dst = publish_base(net, m)
    assert (dst / "Packages" / "bash.rpm").read_bytes() == b"rpm-bytes"
    assert (dst / "repodata" / "repomd.xml").exists()

    # second run: everything already current
    result2 = publish_group([m], net, pool)
    assert result2.linked == 0
    assert result2.skipped == 3


def test_publish_group_release_files_land_last(tmp_path, pool):
    m = _seed_rpm_mirror(pool)
    net = _network(tmp_path / "pub")
    order = []

    def progress(dst, status):
        if status.startswith("linked"):
            order.append(Path(dst).name)

    publish_group([m], net, pool, progress=progress)
    assert order[-1] == "repomd.xml"  # trust root placed after everything else


def test_publish_group_prunes_stale(tmp_path, pool):
    m = _seed_rpm_mirror(pool)
    net = _network(tmp_path / "pub")
    publish_group([m], net, pool)

    stale = publish_base(net, m) / "Packages" / "old.rpm"
    stale.write_bytes(b"stale")

    result = publish_group([m], net, pool, prune=True)
    assert result.pruned == 1
    assert not stale.exists()

    # prune=False leaves stale files alone
    stale.write_bytes(b"stale")
    result = publish_group([m], net, pool, prune=False)
    assert result.pruned == 0
    assert stale.exists()


def test_publish_group_unsynced_mirror(tmp_path, pool):
    m = _mirror("https://example.com/repo/")
    net = _network(tmp_path / "pub")
    result = publish_group([m], net, pool)
    assert result.errors
    assert result.linked == 0


def test_prune_scan_reports_progress_and_prunes_root_level(tmp_path, pool, monkeypatch):
    """The destination walk must report, or a multi-minute scan reads as a hang."""
    from mmw.core.publisher import links as links_mod
    monkeypatch.setattr(links_mod, "_SCAN_REPORT_EVERY", 1)

    m = _seed_rpm_mirror(pool)
    net = _network(tmp_path / "pub")
    publish_group([m], net, pool)

    dst = publish_base(net, m)
    (dst / "stale-at-root").write_bytes(b"x")        # depth 0 — path built from "."
    (dst / "Packages" / "old.rpm").write_bytes(b"x")  # nested

    statuses = []
    result = publish_group([m], net, pool, progress=lambda d, s: statuses.append(s), prune=True)

    assert result.pruned == 2
    assert not (dst / "stale-at-root").exists()
    assert not (dst / "Packages" / "old.rpm").exists()
    assert (dst / "Packages" / "bash.rpm").exists()

    assert statuses[0] == "enumerate-start"
    assert any(s.startswith("enumerate-done:") for s in statuses)
    assert "prune-scan-start" in statuses
    assert any(s.startswith("prune-scan-progress:") for s in statuses)
    done = next(s for s in statuses if s.startswith("prune-scan-done:"))
    _, checked, removed = done.split(":")
    assert int(checked) == 5  # 3 published + 2 stale
    assert int(removed) == 2


def test_prune_scan_keeps_by_hash_entries(tmp_path, pool):
    """by-hash files aren't in `keep` — the walk must skip that subtree, not eat it."""
    m = _mirror("https://deb.example.com/debian/", name="apt-m", provider="apt",
                suite="bookworm", component="main", arch="amd64")
    cs = _seed(pool, b"pkgs-index", "Packages.gz")
    rel = _seed(pool, b"release", "InRelease")
    pool.reconcile_refs("apt-m", [
        (cs, "dists/bookworm/main/binary-amd64/Packages.gz", KIND_META),
        (rel, "dists/bookworm/InRelease", KIND_RELEASE),
    ])
    net = _network(tmp_path / "pub")
    publish_group([m], net, pool)

    bh = publish_base(net, m) / "dists/bookworm/main/binary-amd64/by-hash/SHA256" / cs
    assert bh.exists()
    publish_group([m], net, pool, prune=True)
    assert bh.exists()  # current generation survives the prune walk
