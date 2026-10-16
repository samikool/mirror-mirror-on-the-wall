"""Incremental reference updates preserve desired state and avoid unchanged writes."""
import pytest

from mmw.core.pool import KIND_META, KIND_PKG, Pool


@pytest.fixture(params=[False, True], ids=["inline", "deferred"])
def pool(tmp_path, request):
    with Pool(tmp_path, defer_writes=request.param) as value:
        yield value


def rows(pool, name="unit"):
    with pool._read() as conn:
        return set(conn.execute(
            "SELECT checksum, repo_path, kind, source FROM refs WHERE mirror_name = ?", (name,)))


def test_unchanged_reconcile_does_not_rewrite_refs(pool):
    entries = [("a" * 64, "pool/a.deb", KIND_PKG), ("b" * 64, "dists/Packages.xz", KIND_META)]
    pool.reconcile_refs("unit", entries, "repo")
    before = pool._conn.total_changes
    pool.reconcile_refs("unit", list(reversed(entries)), "repo")
    assert pool._conn.total_changes - before == 0
    assert rows(pool) == {("a" * 64, "pool/a.deb", KIND_PKG, "repo"),
                          ("b" * 64, "dists/Packages.xz", KIND_META, "repo")}


def test_reconcile_changes_only_added_and_removed_rows(pool):
    pool.reconcile_refs("unit", [("a" * 64, "a", KIND_PKG), ("b" * 64, "b", KIND_PKG)])
    before = pool._conn.total_changes
    pool.reconcile_refs("unit", [("a" * 64, "a", KIND_PKG), ("c" * 64, "c", KIND_PKG)])
    assert pool._conn.total_changes - before == 2
    assert rows(pool) == {("a" * 64, "a", KIND_PKG, ""), ("c" * 64, "c", KIND_PKG, "")}


@pytest.mark.parametrize("kind,source", [(KIND_META, "repo"), (KIND_PKG, "new"), (KIND_PKG, "")])
def test_kind_and_source_changes_update_in_place(pool, kind, source):
    pool.reconcile_refs("unit", [("a" * 64, "a", KIND_PKG)], "repo")
    before = pool._conn.total_changes
    pool.reconcile_refs("unit", [("a" * 64, "a", kind)], source)
    assert pool._conn.total_changes - before == 1
    assert rows(pool) == {("a" * 64, "a", kind, source)}


def test_composite_keys_duplicates_and_missing_packages(pool):
    entries = [("a" * 64, "alias", KIND_PKG), ("a" * 64, "second", KIND_PKG),
               ("b" * 64, "alias", KIND_META), ("a" * 64, "alias", KIND_META)]
    pool.reconcile_refs("unit", entries, "repo")
    assert rows(pool) == {("a" * 64, "alias", KIND_PKG, "repo"),
                          ("a" * 64, "second", KIND_PKG, "repo"),
                          ("b" * 64, "alias", KIND_META, "repo")}
    assert set(pool.missing_for("unit")) == {("alias", "a" * 64), ("second", "a" * 64), ("alias", "b" * 64)}
    before = pool._conn.total_changes
    pool.reconcile_refs("unit", entries, "repo")
    assert pool._conn.total_changes == before


def test_empty_target_removes_only_this_mirror(pool):
    entry = ("a" * 64, "a", KIND_PKG)
    pool.reconcile_refs("unit", [entry], "repo")
    pool.reconcile_refs("other", [entry], "other-repo")
    before = pool._conn.total_changes
    pool.reconcile_refs("unit", [])
    assert pool._conn.total_changes - before == 1
    assert rows(pool) == set()
    assert rows(pool, "other") == {("a" * 64, "a", KIND_PKG, "other-repo")}
