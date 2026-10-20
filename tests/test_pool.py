import hashlib
import time

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


def test_a_filename_that_fits_alone_but_not_behind_a_digest_still_pools(pool, mirror_dir):
    # PyPI's zzz…z project: a 211-byte wheel name, 276 bytes behind the checksum.
    name = "z" * 190 + "-0.1.0-py3-none-any.whl"
    checksum = _add(pool, b"wheel", name)
    path = pool.blob_path(checksum, name)
    assert path.read_bytes() == b"wheel"
    assert len(path.name.encode()) == 255 and path.name.startswith(checksum)
    assert pool._blob_strs()(checksum, name) == str(path)   # what listings derive
    assert pool.add_copy(path, checksum, name, 5) == path
    assert not any((mirror_dir / ".pool" / "tmp").iterdir())


def test_fit_name_never_splits_a_character():
    from mmw.core.pool import fit_name
    assert fit_name("a" * 300) == "a" * 255
    assert fit_name("é" * 200) == "é" * 127
    assert fit_name("short.whl") == "short.whl"


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


def _wal_size(pool: Pool) -> int:
    wal = pool.pool_dir / "mmw.db-wal"
    return wal.stat().st_size if wal.exists() else 0


def _add_batch(pool: Pool, tag: str, n: int) -> None:
    for i in range(n):
        _add(pool, f"{tag}-{i}".encode(), f"{tag}-{i}.rpm")
    pool.flush_writes()


def _run_workload(mirror_dir, checkpoint_interval: float) -> int:
    """Same insert workload under a given checkpoint interval. Returns the WAL
    size measured *before* close() (which always TRUNCATEs and would hide
    what we're comparing)."""
    pool = Pool(mirror_dir, checkpoint_interval=checkpoint_interval)
    for round_num in range(4):
        _add_batch(pool, f"round{round_num}", 1000)
        time.sleep(0.2)
    size = _wal_size(pool)
    pool.close()
    return size


def test_periodic_checkpoint_bounds_wal(tmp_path):
    """The writer thread's periodic PASSIVE checkpoint reclaims WAL frames for
    reuse mid-sync, instead of letting the WAL grow unboundedly the way a real
    sync's did (140MB+) — checked here by comparing
    the same workload with checkpointing on vs. effectively off, since the
    packages table's random (sha256) primary key means WAL size per batch
    naturally grows with table size regardless of checkpointing, so a bare
    "does it grow" assertion doesn't isolate the effect."""
    with_checkpoints = _run_workload(tmp_path / "a", checkpoint_interval=0.05)
    without_checkpoints = _run_workload(tmp_path / "b", checkpoint_interval=9999)

    assert with_checkpoints < without_checkpoints, (
        f"periodic checkpointing didn't help: {with_checkpoints} bytes vs "
        f"{without_checkpoints} bytes without it"
    )


def test_close_truncates_wal(mirror_dir):
    pool = Pool(mirror_dir)
    for i in range(50):
        _add(pool, f"row-{i}".encode(), f"f{i}.rpm")
    pool.close()

    wal = mirror_dir / ".pool" / "mmw.db-wal"
    assert not wal.exists() or wal.stat().st_size == 0


# ── read_only pools get the same tuned, dedicated reader as writable ones ────
# See pool.py's `_read()`: verify/dry-run/publish/bundle all open
# `Pool(read_only=True)`, and used to fall back to the bare, untuned `_conn`
# under the same lock a writer would use — so `_PREPARE_WORKERS`-many
# concurrent per-mirror reads (publish_flows._run_per_mirror) serialized on
# one lock with a 2 MB default cache instead of running concurrently against
# a 64 MB-cache, mmap'd reader, the way a writable pool's reads already did.

def test_read_only_pool_with_existing_db_gets_a_tuned_dedicated_reader(mirror_dir):
    Pool(mirror_dir).close()  # create the on-disk db first

    ro = Pool(mirror_dir, read_only=True)
    try:
        assert ro._rconn is not None
        assert ro._rconn is not ro._conn
        cache_size = ro._rconn.execute("PRAGMA cache_size").fetchone()[0]
        # A negative cache_size is in KB (SQLite convention) — the tuned
        # 64 MB setting is -65536; the untuned default is -2000 (2 MB).
        assert cache_size == -65536
    finally:
        ro.close()


def test_read_only_pool_uses_the_dedicated_reader_not_the_write_lock(mirror_dir):
    Pool(mirror_dir).close()

    ro = Pool(mirror_dir, read_only=True)
    try:
        with ro._read() as conn:
            assert conn is ro._rconn
    finally:
        ro.close()


def test_read_only_pool_with_no_db_yet_falls_back_to_in_memory(mirror_dir):
    """Nothing synced yet: there is no file to open a second connection on,
    so this stays the in-memory fallback — unaffected by this fix."""
    ro = Pool(mirror_dir, read_only=True)
    try:
        assert ro._rconn is None
        with ro._read() as conn:
            assert conn is ro._conn
    finally:
        ro.close()
