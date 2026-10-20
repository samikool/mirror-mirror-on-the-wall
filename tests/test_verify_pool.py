"""Phase D: verify_mirror reads the pooled index and checks the packages table."""
import gzip
import hashlib

import pytest

from mmw.core import config
from mmw.core.pool import KIND_META, KIND_PKG, Pool
from mmw.core.verify import verify_mirror


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


def _mirror(provider="apt", **kw) -> config.MirrorConfig:
    defaults = dict(name="m", label="custom", source_url="https://example.com/repo/")
    defaults.update(kw)
    return config.MirrorConfig(provider=provider, **defaults)


def test_verify_apt_complete_and_incomplete(pool, mirror_dir):
    have = _seed(pool, b"present-package", "a.deb")
    missing = "f" * 64
    packages_text = (
        f"Package: a\nFilename: pool/a.deb\nSize: 15\nSHA256: {have}\n\n"
        f"Package: b\nFilename: pool/b.deb\nSize: 10\nSHA256: {missing}\n\n"
    )
    idx = gzip.compress(packages_text.encode())
    idx_cs = hashlib.sha256(idx).hexdigest()
    pool.add(idx, idx_cs, "sha256", "Packages.gz", len(idx))
    m = _mirror("apt", suite="bw", component="main", arch="amd64")
    pool.reconcile_refs("m", [
        (have, "pool/a.deb", KIND_PKG),
        (missing, "pool/b.deb", KIND_PKG),
        (idx_cs, "dists/bw/main/binary-amd64/Packages.gz", KIND_META),
    ])

    r = verify_mirror(m, mirror_dir, pool)
    assert r.total == 2
    assert r.present == 1
    assert r.missing_count == 1
    assert not r.complete

    # once the missing package lands in the pool, the mirror verifies complete
    # (pool.add trusts the caller-supplied checksum; membership is what we test)
    pool.add(b"placeholder", missing, "sha256", "b.deb", 11)
    r2 = verify_mirror(m, mirror_dir, pool)
    assert r2.complete


def test_verify_apt_unsynced(pool, mirror_dir):
    r = verify_mirror(_mirror("apt", suite="bw", component="main"), mirror_dir, pool)
    assert r.error
    assert "synced" in r.error




def test_deep_verify_finds_a_truncated_blob_and_repair_refetches_it(pool, mirror_dir):
    """A blob truncated under its own (correct) checksum is invisible to every
    database check, and a sync never re-fetches a pooled checksum — so the
    damage is permanent until something stats the file and drops the row.
    That is `mmw verify --stat --repair`; afterwards the ref is simply
    missing, which is what the next sync fills."""
    content = b"x" * 4096
    cs = _seed(pool, content, "a.deb")
    pool.reconcile_refs("m", [(cs, "pool/a.deb", KIND_PKG)])
    blob = pool.pool_dir / "packages" / cs[:2] / f"{cs}-a.deb"
    blob.write_bytes(content[:100])

    m = _mirror("apt-flat")
    assert verify_mirror(m, mirror_dir, pool).corrupt == []          # shallow: blind to it
    r = verify_mirror(m, mirror_dir, pool, stat_blobs=True)
    assert r.corrupt_checksums == [cs] and not r.complete

    assert pool.forget_blobs(r.corrupt_checksums) == (1, len(content))
    assert not blob.exists()
    assert pool.batch_contains([cs]) == set()                          # the sync will fetch it
    assert pool.missing_for("m") == [("pool/a.deb", cs)]


def test_rehash_finds_a_blob_rewritten_at_the_same_size(pool, mirror_dir):
    cs = _seed(pool, b"good-bytes", "a.deb")
    pool.reconcile_refs("m", [(cs, "pool/a.deb", KIND_PKG)])
    (pool.pool_dir / "packages" / cs[:2] / f"{cs}-a.deb").write_bytes(b"evil-bytes")

    m = _mirror("apt-flat")
    assert verify_mirror(m, mirror_dir, pool, stat_blobs=True).corrupt == []
    assert verify_mirror(m, mirror_dir, pool, hash_blobs=True).corrupt_checksums == [cs]


def _twelve_blobs(pool):
    entries = []
    for i in range(12):
        cs = _seed(pool, f"blob {i}".encode(), f"{i}.deb")
        entries.append((cs, f"pool/{i}.deb", KIND_PKG))
    pool.reconcile_refs("m", entries)
    return entries


def test_blob_stat_is_concurrent(pool, mirror_dir, monkeypatch, disk_limits):
    """Serial-fails: the stat of row 0 waits on an event only row 5's stat sets."""
    import threading
    from mmw.core import disk_io
    _twelve_blobs(pool)
    rows = pool.mirror_files(["m"])
    first, sixth = rows[0][1], rows[5][1]
    row5, timed_out, real = threading.Event(), [], disk_io.ops.stat

    def stat(path):
        if str(path) == first:
            timed_out.append(not row5.wait(5))
        if str(path) == sixth:
            row5.set()
        return real(path)

    monkeypatch.setattr(disk_io.ops, "stat", stat)
    disk_limits(read_threads=8)
    r = verify_mirror(_mirror("apt-flat"), mirror_dir, pool, stat_blobs=True)
    assert timed_out == [False]
    assert r.checked_blobs == 12 and r.corrupt == []
    assert r.timings["disk read-meta"] > 0


def test_blob_stat_reports_progress(pool, mirror_dir, monkeypatch):
    from mmw.core import verify
    monkeypatch.setattr(verify, "_STAT_REPORT_EVERY", 5)
    _twelve_blobs(pool)
    said = []
    verify_mirror(_mirror("apt-flat"), mirror_dir, pool, stat_blobs=True, say=said.append)
    assert [s for s in said if s.startswith("stat ")] == [
        "stat 5/12 blobs…", "stat 10/12 blobs…", "stat 12/12 blobs…"]


def test_verify_stops_mid_blob_check(pool, mirror_dir, monkeypatch):
    import threading
    from mmw.core import verify
    monkeypatch.setattr(verify, "_STAT_REPORT_EVERY", 5)
    _twelve_blobs(pool)
    stop = threading.Event()

    def say(text):
        if text.startswith("stat 5/"):
            stop.set()

    r = verify_mirror(_mirror("apt-flat"), mirror_dir, pool, stat_blobs=True, say=say,
                      stop_event=stop)
    assert r.error == "stopped"
    assert r.checked_blobs < 12


def test_verify_hash_detects_rewritten_blob_with_threads(pool, mirror_dir, disk_limits):
    entries = _twelve_blobs(pool)
    bad = entries[4][0]
    (pool.pool_dir / "packages" / bad[:2] / f"{bad}-4.deb").write_bytes(b"blob X")  # same size
    m = _mirror("apt-flat")
    serial = verify_mirror(m, mirror_dir, pool, hash_blobs=True)
    disk_limits(read_threads=8, readahead=4)
    threaded = verify_mirror(m, mirror_dir, pool, hash_blobs=True)
    assert "disk stream" in threaded.timings
    assert serial.corrupt_checksums == threaded.corrupt_checksums == [bad]
    assert serial.corrupt == threaded.corrupt


def test_verify_hash_reports_progress(pool, mirror_dir, monkeypatch):
    from mmw.core import verify
    monkeypatch.setattr(verify, "_STAT_REPORT_EVERY", 5)
    _twelve_blobs(pool)
    said = []
    verify_mirror(_mirror("apt-flat"), mirror_dir, pool, hash_blobs=True, say=said.append)
    assert [s for s in said if s.startswith("hash ")] == [
        "hash 5/12 blobs…", "hash 10/12 blobs…", "hash 12/12 blobs…"]
