"""Snapshots in the pool database: rows in, retention against prune, freed
again once the snapshot is deleted."""
import hashlib

import pytest

from mmw.core.pool import KIND_PKG, Pool

SRC = "https://deb.example.com/debian"


def _add(pool, data: bytes, name: str) -> str:
    cs = hashlib.sha256(data).hexdigest()
    pool.add(data, cs, "sha256", name, len(data))
    return cs


@pytest.fixture
def pool(tmp_path):
    p = Pool(tmp_path / "mirror")
    yield p
    p.close()


def test_a_snapshot_round_trips_its_rows(pool):
    sid = pool.create_snapshot("oak", "20261011T120000Z", label="pre-upgrade", kind="manual",
                               rows=[("a/pool/x.deb", "c1"), ("a/Packages", "c2")])
    [info] = pool.snapshots("oak")
    assert (info.id, info.stamp, info.label, info.kind, info.complete, info.files) == \
        (sid, "20261011T120000Z", "pre-upgrade", "manual", True, 2)
    assert dict(pool.snapshot_files(sid)) == {"a/pool/x.deb": "c1", "a/Packages": "c2"}
    assert pool.snapshots("elm") == []


def test_prune_keeps_what_a_snapshot_holds_and_frees_it_once_deleted(pool):
    old = _add(pool, b"old version", "x_1.deb")
    new = _add(pool, b"new version", "x_2.deb")
    pool.reconcile_refs("m", [(old, "pool/x_1.deb", KIND_PKG)], source=SRC)
    sid = pool.create_snapshot("oak", "20261011T120000Z", kind="bundle",
                               rows=[("deb/pool/x_1.deb", old)])
    # Upstream moved on: the live mirror now wants only the new file.
    pool.reconcile_refs("m", [(new, "pool/x_2.deb", KIND_PKG)], source=SRC)

    assert pool.prune_orphans([SRC]) == (0, 0)          # the snapshot holds the old one
    assert pool.contains(old) and pool.contains(new)
    assert pool.snapshot_exclusive_bytes(sid) == len(b"old version")

    pool.delete_snapshot(sid)
    assert pool.prune_orphans([SRC]) == (1, len(b"old version"))
    assert not pool.contains(old) and pool.contains(new)


def test_renaming_and_two_networks_sharing_a_stamp(pool):
    a = pool.create_snapshot("oak", "20261011T120000Z", kind="manual", rows=[])
    pool.create_snapshot("elm", "20261011T120000Z", kind="manual", rows=[])
    pool.rename_snapshot(a, "golden")
    assert [s.label for s in pool.snapshots("oak")] == ["golden"]
    with pytest.raises(ValueError, match="already"):
        pool.create_snapshot("oak", "20261011T120000Z", kind="manual", rows=[])
