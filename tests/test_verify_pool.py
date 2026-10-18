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
