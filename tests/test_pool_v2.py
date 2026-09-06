"""Pool-first redesign Phase A: blobs + desired-state refs."""
import hashlib
import sqlite3
import threading
import time

import pytest

from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool


@pytest.fixture
def mirror_dir(tmp_path):
    return tmp_path / "mirror"


@pytest.fixture
def pool(mirror_dir):
    p = Pool(mirror_dir)
    yield p
    p.close()


def _blob(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _add(pool, content: bytes, name: str) -> str:
    checksum = _blob(content)
    pool.add(content, checksum, "sha256", name, len(content))
    return checksum


# ── add() ─────────────────────────────────────────────────────────────────────

def test_add_from_bytes(pool):
    checksum = _add(pool, b"blob-bytes", "a.deb")
    pool_file = pool.pool_dir / "packages" / checksum[:2] / f"{checksum}-a.deb"
    assert pool_file.read_bytes() == b"blob-bytes"
    assert pool.contains(checksum)
    assert not list(pool._tmp_dir.iterdir())  # tmp cleaned up


def test_add_from_tmp_file(pool, tmp_path):
    content = b"from-file"
    checksum = _blob(content)
    tmp = pool._tmp_dir / "dl.tmp"
    tmp.write_bytes(content)
    dest = pool.add(tmp, checksum, "sha256", "b.rpm", len(content))
    assert dest.read_bytes() == content
    assert not tmp.exists()


def test_add_existing_checksum_reused(pool):
    checksum = _add(pool, b"same", "c.deb")
    ino = (pool.pool_dir / "packages" / checksum[:2] / f"{checksum}-c.deb").stat().st_ino
    _add(pool, b"same", "c.deb")  # second add is a no-op on the file
    assert (pool.pool_dir / "packages" / checksum[:2] / f"{checksum}-c.deb").stat().st_ino == ino


# ── reconcile_refs ────────────────────────────────────────────────────────────

def test_reconcile_replaces_exactly(pool):
    a = _add(pool, b"a", "a.deb")
    b = _add(pool, b"b", "b.deb")
    c = _add(pool, b"c", "c.deb")

    pool.reconcile_refs("m1", [(a, "pool/a.deb", KIND_PKG), (b, "pool/b.deb", KIND_PKG)])
    assert {r[0] for r in pool.mirror_files(["m1"])} == {"pool/a.deb", "pool/b.deb"}

    # next sync: a dropped upstream, c added
    pool.reconcile_refs("m1", [(b, "pool/b.deb", KIND_PKG), (c, "pool/c.deb", KIND_PKG)])
    assert {r[0] for r in pool.mirror_files(["m1"])} == {"pool/b.deb", "pool/c.deb"}


def test_reconcile_does_not_touch_other_mirrors(pool):
    a = _add(pool, b"a", "a.deb")
    pool.reconcile_refs("m1", [(a, "a.deb", KIND_PKG)])
    pool.reconcile_refs("m2", [(a, "a.deb", KIND_PKG)])
    pool.reconcile_refs("m1", [])
    assert pool.mirror_files(["m1"]) == []
    assert len(pool.mirror_files(["m2"])) == 1


def test_reconcile_can_include_missing_packages(pool):
    """refs are desired state — entries without pool content are legal."""
    have = _add(pool, b"have", "have.deb")
    pool.reconcile_refs("m1", [
        (have, "pool/have.deb", KIND_PKG),
        ("0" * 64, "pool/want.deb", KIND_PKG),  # wanted, not downloaded
    ])
    # mirror_files only returns what exists
    assert [r[0] for r in pool.mirror_files(["m1"])] == ["pool/have.deb"]
    # missing_for reports the gap
    assert pool.missing_for("m1") == [("pool/want.deb", "0" * 64)]


# ── mirror_files ──────────────────────────────────────────────────────────────

def test_mirror_files_dedup_order_and_kinds(pool):
    pkg = _add(pool, b"pkg", "x.deb")
    idx = _add(pool, b"index", "Packages.xz")
    rel = _add(pool, b"release", "InRelease")

    for m in ("deb-main", "deb-contrib"):
        pool.reconcile_refs(m, [
            (pkg, "pool/z/x.deb", KIND_PKG),
            (idx, f"dists/bw/{m.split('-')[1]}/Packages.xz", KIND_META),
            (rel, "dists/bw/InRelease", KIND_RELEASE),
        ])

    rows = pool.mirror_files(["deb-main", "deb-contrib"])
    paths = [r[0] for r in rows]
    assert paths == sorted(paths)                      # ordered
    assert paths.count("dists/bw/InRelease") == 1      # shared file deduped
    assert paths.count("pool/z/x.deb") == 1

    pkg_rows = pool.mirror_files(["deb-main", "deb-contrib"], kinds=(KIND_PKG,))
    assert [r[0] for r in pkg_rows] == ["pool/z/x.deb"]
    rel_rows = pool.mirror_files(["deb-main"], kinds=(KIND_RELEASE,))
    assert [r[0] for r in rel_rows] == ["dists/bw/InRelease"]
    assert pool.mirror_files([]) == []


def test_resolve(pool):
    idx = _add(pool, b"index-bytes", "Packages.xz")
    pool.reconcile_refs("m1", [(idx, "dists/bw/main/Packages.xz", KIND_META)])
    p = pool.resolve("m1", "dists/bw/main/Packages.xz")
    assert p is not None and p.read_bytes() == b"index-bytes"
    assert pool.resolve("m1", "nope") is None
    assert pool.resolve("m2", "dists/bw/main/Packages.xz") is None


# ── stats honor kind ──────────────────────────────────────────────────────────

def test_mirror_stats_exclude_metadata(pool):
    pkg = _add(pool, b"pkg-bytes!", "x.deb")     # 10 bytes
    idx = _add(pool, b"index", "Packages.xz")
    pool.reconcile_refs("m1", [
        (pkg, "pool/x.deb", KIND_PKG),
        (idx, "dists/Packages.xz", KIND_META),
    ])
    assert pool.mirror_stats("m1") == (1, 10)
    assert pool.group_unique_count(["m1"]) == 1


# ── prune_orphans ─────────────────────────────────────────────────────────────

def test_prune_orphans_removes_unwanted(pool):
    keep = _add(pool, b"keep", "keep.deb")
    old = _add(pool, b"old-version", "old.deb")
    pool.reconcile_refs("m1", [(keep, "pool/keep.deb", KIND_PKG)])
    # `old` has no refs (superseded upstream)

    removed, freed = pool.prune_orphans(["m1"])
    assert removed == 1
    assert freed == len(b"old-version")
    assert pool.contains(keep)
    assert not pool.contains(old)
    assert not (pool.pool_dir / "packages" / old[:2] / f"{old}-old.deb").exists()


KEPT = "http://archive.example.com/ubuntu"
GONE = "http://old.example.com/debian"


def test_prune_orphans_gcs_unconfigured_repositories(pool):
    a = _add(pool, b"a", "a.deb")
    b = _add(pool, b"b", "b.deb")
    pool.reconcile_refs("kept-noble-main-amd64", [(a, "a.deb", KIND_PKG)], KEPT)
    pool.reconcile_refs("gone-bookworm-main-amd64", [(b, "b.deb", KIND_PKG)], GONE)

    removed, _ = pool.prune_orphans([KEPT])
    assert removed == 1
    assert pool.contains(a)
    assert not pool.contains(b)
    assert pool.mirror_files(["gone-bookworm-main-amd64"]) == []


def test_prune_keeps_every_arch_of_a_configured_repository(pool):
    """The bug this column exists for: `arches = []` means "all", so the
    configured *mirror* list is narrower than what was synced. Pruning on
    mirror names would delete every arch the config does not spell out."""
    checksums = {}
    for arch in ("amd64", "i386", "arm64"):
        cs = _add(pool, f"pkg-{arch}".encode(), f"{arch}.deb")
        checksums[arch] = cs
        pool.reconcile_refs(f"ubuntu-noble-main-{arch}", [(cs, f"{arch}.deb", KIND_PKG)], KEPT)

    removed, _ = pool.prune_orphans([KEPT])
    assert removed == 0
    for arch, cs in checksums.items():
        assert pool.contains(cs), f"{arch} was pruned"


def test_prune_never_touches_rows_with_no_source(pool):
    """Pre-source rows cannot prove they are unwanted, so a destructive
    operation must leave them alone; the next sync gives them a source."""
    a = _add(pool, b"a", "a.deb")
    pool.reconcile_refs("legacy-mirror", [(a, "a.deb", KIND_PKG)])   # no source
    removed, _ = pool.prune_orphans([KEPT])
    assert removed == 0
    assert pool.contains(a)


def test_pool_enumerates_what_was_actually_synced(pool):
    a = _add(pool, b"a", "a.deb")
    b = _add(pool, b"b", "b.deb")
    pool.reconcile_refs("ubuntu-noble-main-amd64", [(a, "a.deb", KIND_PKG)], KEPT)
    pool.reconcile_refs("ubuntu-noble-main-i386", [(b, "b.deb", KIND_PKG)], KEPT)

    assert pool.list_mirrors() == ["ubuntu-noble-main-amd64", "ubuntu-noble-main-i386"]
    assert pool.list_sources() == [KEPT]
    assert pool.mirrors_for_source(KEPT) == [
        "ubuntu-noble-main-amd64", "ubuntu-noble-main-i386",
    ]


def test_prune_orphans_metadata_blobs_too(pool):
    old_idx = _add(pool, b"old-index", "Packages.xz")
    new_idx = _add(pool, b"new-index", "Packages.xz")
    pool.reconcile_refs("m1", [(new_idx, "dists/Packages.xz", KIND_META)])
    removed, _ = pool.prune_orphans(["m1"])
    assert removed == 1
    assert not pool.contains(old_idx)
    assert pool.contains(new_idx)


# ── legacy DB upgrade + tmp reaping ──────────────────────────────────────────

def test_pre_kind_db_upgraded(tmp_path):
    """Opening a refs table created without `kind` adds the column in place."""
    mirror_dir = tmp_path / "mirror"
    pool_dir = mirror_dir / ".pool"
    pool_dir.mkdir(parents=True)
    conn = sqlite3.connect(pool_dir / "mmw.db")
    conn.executescript("""
        CREATE TABLE packages (checksum TEXT PRIMARY KEY, checksum_type TEXT NOT NULL,
            filename TEXT NOT NULL, size INTEGER NOT NULL, pool_path TEXT NOT NULL,
            added_at TEXT);
        CREATE TABLE refs (checksum TEXT NOT NULL, mirror_name TEXT NOT NULL,
            repo_path TEXT NOT NULL, PRIMARY KEY (checksum, mirror_name, repo_path));
        INSERT INTO packages VALUES ('c1', 'sha256', 'a.deb', 5, '/x/a.deb', NULL);
        INSERT INTO refs VALUES ('c1', 'm1', 'pool/a.deb');
    """)
    conn.commit()
    conn.close()

    with Pool(mirror_dir) as pool:
        # legacy rows default to kind='pkg'
        assert pool.mirror_stats("m1") == (1, 5)
        rows = pool.mirror_files(["m1"], kinds=("pkg",))
        assert [r[0] for r in rows] == ["pool/a.deb"]


def test_stale_tmp_reaped_fresh_kept(mirror_dir):
    with Pool(mirror_dir) as p1:
        stale = p1._tmp_dir / "stale.tmp"
        fresh = p1._tmp_dir / "fresh.tmp"
        stale.write_bytes(b"x")
        fresh.write_bytes(b"y")
        old = time.time() - 100_000
        import os
        os.utime(stale, (old, old))
    with Pool(mirror_dir) as p2:
        assert not (p2._tmp_dir / "stale.tmp").exists()
        assert (p2._tmp_dir / "fresh.tmp").exists()


def test_concurrent_reconcile_and_read(pool):
    """Readers never see a half-reconciled mirror (single transaction)."""
    blobs = [_add(pool, f"v{i}".encode(), f"p{i}.deb") for i in range(20)]
    pool.reconcile_refs("m1", [(blobs[i], f"p{i}.deb", KIND_PKG) for i in range(10)])

    errors = []

    def flip():
        for _ in range(30):
            pool.reconcile_refs("m1", [(blobs[i], f"p{i}.deb", KIND_PKG) for i in range(10)])
            pool.reconcile_refs("m1", [(blobs[i], f"p{i}.deb", KIND_PKG) for i in range(10, 20)])

    def read():
        for _ in range(60):
            n = len(pool.mirror_files(["m1"]))
            if n != 10:  # must always be one complete set, never a mix/partial
                errors.append(n)

    t1, t2 = threading.Thread(target=flip), threading.Thread(target=read)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert errors == []
