import hashlib

import pytest

from mmw.core.pool import KIND_PKG, Pool, verify_checksum, wipe


@pytest.fixture
def mirror_dir(tmp_path):
    return tmp_path / "mirror"


@pytest.fixture
def pool(mirror_dir):
    p = Pool(mirror_dir)
    yield p
    p.close()


def _add(pool: Pool, content: bytes, name: str) -> str:
    checksum = hashlib.sha256(content).hexdigest()
    pool.add(content, checksum, "sha256", name, len(content))
    return checksum


def test_contains_and_batch_contains(pool):
    checksum = _add(pool, b"x", "a.rpm")
    assert pool.contains(checksum)
    assert not pool.contains("0" * 64)
    assert pool.batch_contains([checksum, "missing"]) == {checksum}
    assert pool.batch_contains([]) == set()


def test_add_upsert_updates_pool_path_for_new_filename(pool):
    """Same checksum re-added under a new filename → DB follows the live file."""
    content = b"same-bytes"
    checksum = _add(pool, content, "old-name.rpm")
    _add(pool, content, "new-name.rpm")

    pool.reconcile_refs("m", [(checksum, "x.rpm", KIND_PKG)])
    rows = pool.mirror_files(["m"])
    assert len(rows) == 1
    assert rows[0][1].endswith("new-name.rpm")  # pool_path points at the live file


def test_mirror_stats(pool):
    a = _add(pool, b"aa", "a.rpm")
    b = _add(pool, b"bbbb", "b.rpm")
    pool.reconcile_refs("m1", [(a, "a.rpm", KIND_PKG), (b, "b.rpm", KIND_PKG)])
    assert pool.mirror_stats("m1") == (2, 6)
    assert pool.mirror_stats("nope") == (0, 0)


def test_group_unique_count_dedupes_across_mirrors(pool):
    shared = _add(pool, b"zz", "shared.rpm")
    pool.reconcile_refs("m1", [(shared, "shared.rpm", KIND_PKG)])
    pool.reconcile_refs("m2", [(shared, "shared.rpm", KIND_PKG)])
    assert pool.group_unique_count(["m1", "m2"]) == 1
    assert pool.group_unique_count([]) == 0


def test_stats(pool):
    _add(pool, b"12345", "a.rpm")
    assert pool.stats() == {"packages": 1, "total_size": 5}


def test_wipe(pool, mirror_dir):
    _add(pool, b"x", "a.rpm")
    pool.close()
    wipe(mirror_dir)
    assert not (mirror_dir / ".pool").exists()


def test_verify_checksum(tmp_path):
    f = tmp_path / "f"
    f.write_bytes(b"hello")
    good = hashlib.sha256(b"hello").hexdigest()
    assert verify_checksum(f, good, "sha256")
    assert not verify_checksum(f, "0" * 64, "sha256")
