"""Incremental reference updates preserve desired state and avoid unchanged writes."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import sqlite3
import threading

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


@pytest.mark.parametrize("target", [[], [("c" * 64, "c", KIND_PKG), ("d" * 64, "d", KIND_PKG)]])
def test_removing_all_old_rows_uses_one_delete(pool, target):
    pool.reconcile_refs("unit", [("a" * 64, "a", KIND_PKG), ("b" * 64, "b", KIND_PKG)])
    statements = []
    pool._conn.set_trace_callback(statements.append)
    pool.reconcile_refs("unit", target)
    pool._conn.set_trace_callback(None)
    assert len([s for s in statements if s.startswith("DELETE FROM refs")]) == 1
    assert rows(pool) == {(cs, path, kind, "") for cs, path, kind in target}


def reject_replacement(pool):
    pool._conn.executescript("""
        CREATE TEMP TRIGGER reject_new_ref BEFORE INSERT ON main.refs
        WHEN NEW.repo_path = 'new.deb'
        BEGIN SELECT RAISE(ABORT, 'injected reconcile failure'); END;
    """)


def test_failed_reconcile_rolls_back_and_connection_recovers(pool):
    pool.reconcile_refs("unit", [("a" * 64, "old.deb", KIND_PKG)], "old")
    reject_replacement(pool)
    with pytest.raises(sqlite3.IntegrityError, match="injected reconcile failure"):
        pool.reconcile_refs("unit", [("b" * 64, "new.deb", KIND_PKG)], "new")
    assert rows(pool) == {("a" * 64, "old.deb", KIND_PKG, "old")}
    assert not pool._conn.in_transaction
    pool._conn.execute("DROP TRIGGER reject_new_ref")
    pool.reconcile_refs("unit", [("b" * 64, "new.deb", KIND_PKG)], "new")
    assert rows(pool) == {("b" * 64, "new.deb", KIND_PKG, "new")}


def test_reader_sees_old_state_until_entire_transaction_commits(pool):
    pool.reconcile_refs("unit", [("a" * 64, "old.deb", KIND_PKG)], "old")
    paused, release = threading.Event(), threading.Event()
    def pause():
        paused.set()
        assert release.wait(5), "reader never released the paused transaction"
        return 0
    pool._conn.create_function("pause_reconcile", 0, pause)
    pool._conn.executescript("""
        CREATE TEMP TRIGGER pause_new_ref BEFORE INSERT ON main.refs
        BEGIN SELECT pause_reconcile(); END;
    """)
    with ThreadPoolExecutor(1) as executor:
        pending = executor.submit(pool.reconcile_refs, "unit", [("b" * 64, "new.deb", KIND_PKG)], "new")
        try:
            assert paused.wait(5), "writer did not reach the paused mutation"
            assert rows(pool) == {("a" * 64, "old.deb", KIND_PKG, "old")}
            assert not pending.done()
        finally:
            release.set()
        pending.result(timeout=5)
    assert rows(pool) == {("b" * 64, "new.deb", KIND_PKG, "new")}


@pytest.mark.parametrize("fail", [False, True])
def test_deferred_completion_follows_blob_writes_and_atomic_commit(tmp_path, fail):
    with Pool(tmp_path, defer_writes=True) as pool:
        pool.reconcile_refs("unit", [("a" * 64, "old.deb", KIND_PKG)], "old")
        if fail:
            reject_replacement(pool)
        completed = threading.Event()
        observations = []
        digest = hashlib.sha256(b"download").hexdigest()
        def done(error):
            try:
                observations.append((error, rows(pool), pool.batch_contains([digest])))
            finally:
                completed.set()
        # Hold the writer lock while queuing so this genuinely tests pending
        # blobs rather than racing a tiny add that might already have committed.
        with pool._lock:
            pool.add(b"download", digest, "sha256", "new.deb", 8)
            assert pool.submit_reconcile("unit", [(digest, "new.deb", KIND_PKG)], "new", done)
            assert not completed.is_set()
        assert completed.wait(5)
        assert len(observations) == 1
        error, visible_refs, visible_blobs = observations[0]
        assert visible_blobs == {digest}
        assert not pool._conn.in_transaction
        if fail:
            assert isinstance(error, sqlite3.IntegrityError)
            assert visible_refs == {("a" * 64, "old.deb", KIND_PKG, "old")}
            assert any("injected reconcile failure" in e for e in pool.take_write_errors())
        else:
            assert error is None
            assert visible_refs == {(digest, "new.deb", KIND_PKG, "new")}
