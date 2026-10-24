"""Repository versions and the per-repository bundle ledger, in the pool DB."""
import sqlite3

import pytest

from mmw.core.pool import Pool


@pytest.fixture
def pool(tmp_path):
    with Pool(tmp_path / "m") as p:
        yield p


def _snap(pool, stamp, network="n"):
    return pool.create_snapshot(network, stamp, kind="manual", rows=[])


def test_versions_numbered_and_reused(pool):
    s1 = _snap(pool, "20261001T000000Z")
    v = pool.record_repo_versions(s1, "n", "20261001T000000Z", {"a": "d1", "b": "d2"})
    assert (v["a"].number, v["b"].number) == (1, 1)
    s2 = _snap(pool, "20261002T000000Z")
    v2 = pool.record_repo_versions(s2, "n", "20261002T000000Z", {"a": "d1", "b": "d3"})
    assert v2["a"].id == v["a"].id and v2["b"].number == 2
    assert v2["b"].stamp == "20261002T000000Z" and v2["a"].stamp == "20261001T000000Z"
    assert [x.number for x in pool.repo_versions("n", "b")] == [2, 1]
    assert pool.repo_version_snapshots(v["a"].id) == [s2, s1]
    assert pool.snapshot_repo_versions(s2) == v2
    assert pool.repo_version(v2["b"].id) == v2["b"]
    with pytest.raises(LookupError):
        pool.repo_version(99999)


def test_versions_are_per_network(pool):
    a = pool.record_repo_versions(_snap(pool, "20261001T000000Z", "n"), "n", "20261001T000000Z", {"r": "d"})
    b = pool.record_repo_versions(_snap(pool, "20261001T000000Z", "m"), "m", "20261001T000000Z", {"r": "d"})
    assert a["r"].id != b["r"].id and b["r"].number == 1


def test_bundle_repos_ledger_and_kind(pool):
    s1 = _snap(pool, "20261001T000000Z")
    v1 = pool.record_repo_versions(s1, "n", "20261001T000000Z", {"a": "a1", "b": "b1"})
    s2 = _snap(pool, "20261002T000000Z")
    v2 = pool.record_repo_versions(s2, "n", "20261002T000000Z", {"a": "a1", "b": "b2"})
    first = pool.record_bundle("n", None, "20261001T000000Z", "/o/x.tar", 2, 10, 0,
                               repos={"a": (None, v1["a"].id), "b": (None, v1["b"].id)})
    pool.record_bundle("n", "20261001T000000Z", "20261002T000000Z", "/o/stage", 1, 5, 0,
                       kind="stage", repos={"b": (v1["b"].id, v2["b"].id)})
    assert isinstance(first, int)
    assert pool.last_sent_versions("n") == {"a": v1["a"].id, "b": v2["b"].id}
    assert [b.kind for b in pool.bundles("n")] == ["stage", "bundle"]


def test_delete_snapshot_keeps_version_rows(pool):
    s1 = _snap(pool, "20261001T000000Z")
    v = pool.record_repo_versions(s1, "n", "20261001T000000Z", {"a": "d1"})
    pool.delete_snapshot(s1)
    assert pool.repo_version(v["a"].id) == v["a"]
    assert pool.repo_version_snapshots(v["a"].id) == []


def test_forget_network_drops_versions_and_ledger(pool):
    s1 = _snap(pool, "20261001T000000Z")
    v = pool.record_repo_versions(s1, "n", "20261001T000000Z", {"a": "d1"})
    pool.record_bundle("n", None, "20261001T000000Z", "/o", 1, 1, 0, repos={"a": (None, v["a"].id)})
    pool.forget_network("n")
    assert pool.repo_versions("n") == [] and pool.last_sent_versions("n") == {}


def test_rename_network_carries_versions(pool):
    s1 = _snap(pool, "20261001T000000Z")
    pool.record_repo_versions(s1, "n", "20261001T000000Z", {"a": "d1"})
    assert pool.rename_network("n", "m") is None
    assert [v.network for v in pool.repo_versions("m")] == ["m"]


def test_existing_db_migrates_bundles_kind(tmp_path):
    with Pool(tmp_path / "m") as p:
        db = p.db_path
    conn = sqlite3.connect(db)                       # the previous schema: no kind column
    conn.executescript("DROP TABLE bundles; CREATE TABLE bundles (id INTEGER PRIMARY KEY, network TEXT NOT NULL, "
                       "from_stamp TEXT, to_stamp TEXT NOT NULL, out_path TEXT NOT NULL, sent INTEGER NOT NULL "
                       "DEFAULT 0, sent_bytes INTEGER NOT NULL DEFAULT 0, removed INTEGER NOT NULL DEFAULT 0, "
                       "made TEXT NOT NULL DEFAULT (strftime('%Y%m%dT%H%M%SZ', 'now')));"
                       "INSERT INTO bundles (network, to_stamp, out_path) VALUES ('n', 's', '/o');"
                       "DROP TABLE repo_versions; DROP TABLE snapshot_repos; DROP TABLE bundle_repos;")
    conn.commit()
    conn.close()
    with Pool(tmp_path / "m", read_only=True) as p:      # read-only before any migration
        assert [b.kind for b in p.bundles("n")] == ["bundle"]
        assert p.repo_versions("n") == [] and p.last_sent_versions("n") == {}
    with Pool(tmp_path / "m") as p:
        assert [b.kind for b in p.bundles("n")] == ["bundle"]
