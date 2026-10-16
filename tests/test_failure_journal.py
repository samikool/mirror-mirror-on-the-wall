"""Failure recovery scales with the journal, not with the full mirror."""
# ruff: noqa: F811
import asyncio
import concurrent.futures
import hashlib
import json
import sqlite3

import httpx
import pytest

from mmw.core import config, downloader
from mmw.core.pool import Pool
from mmw.core.sync_engine import sync_mirror
from mmw.providers.base import MetadataFile, PackageInfo
from test_retry_failed import incomplete, repair  # noqa: F401


def no_scan(*args, **kwargs):
    raise AssertionError("retry must not enumerate the mirror or read metadata")


def test_fast_retry_reads_only_failures_and_deletes_success(incomplete, tmp_path, monkeypatch):
    _, unit, missing, blocked, upstream = incomplete
    blocked.clear()
    with Pool(tmp_path) as pool:
        token, failures = pool.retry_plan(unit.name)
        assert len(failures) == 1
        assert failures[0]["package"]["url"] == missing.url
        assert failures[0]["error"]
        monkeypatch.setattr(pool, "ref_map", no_scan)
        monkeypatch.setattr(pool, "mirror_files", no_scan)
        from pathlib import Path
        monkeypatch.setattr(Path, "read_bytes", no_scan)
        sql = []
        pool._conn.set_trace_callback(sql.append)
        pool._rconn.set_trace_callback(sql.append)
        assert repair(pool, unit).downloaded == 1
        assert pool.retry_plan(unit.name) == (token, [])
        assert repair(pool, unit).total == 0
        assert [str(r.url) for r in upstream.requests] == [missing.url]
        assert not any("SELECT" in q and "FROM refs" in q for q in sql)
        assert pool._conn.execute("SELECT COUNT(*) FROM retry_failures").fetchone()[0] == 0


def test_legacy_mirror_scans_once_then_uses_saved_list(incomplete, tmp_path, monkeypatch):
    _, unit, _, blocked, _ = incomplete
    with Pool(tmp_path) as pool:
        pool._conn.execute("DELETE FROM retry_state")
        pool._conn.execute("DELETE FROM retry_failures")
        pool._conn.commit()
        assert repair(pool, unit).failed == 1
        assert len(pool.retry_plan(unit.name)[1]) == 1
    with Pool(tmp_path) as pool:
        monkeypatch.setattr(pool, "ref_map", no_scan)
        monkeypatch.setattr(pool, "mirror_files", no_scan)
        blocked.clear()
        assert repair(pool, unit).ok
        assert pool.retry_plan(unit.name)[1] == []


@pytest.mark.parametrize("deferred", [False, True])
def test_partial_success_removes_only_successes_and_survives_reopen(tmp_path, monkeypatch, deferred):
    unit = config.MirrorConfig(name="extensions", provider="vscode", source_url=config.MARKETPLACE_URL,
                               source=config.MARKETPLACE_URL, dims={"kind": "extensions"})
    packages = [PackageInfo(str(i), f"https://assets.test/{i}", "", "sha256", 0,
                            f"assets.test/{i}", str(i), checksum_known=False) for i in range(4)]
    allowed = set()
    requests = []

    def respond(req):
        url = str(req.url)
        requests.append(url)
        return httpx.Response(200, content=url.encode()) if url in allowed else httpx.Response(404)

    monkeypatch.setattr(downloader, "_thread_client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    witness = json.dumps({"versions": [{"files": [{"source": p.url} for p in packages]}]}).encode()
    mf = MetadataFile("", ".vscode-meta/extensions/universal/test.json", content=witness,
                      expected_sha256=hashlib.sha256(witness).hexdigest())
    with Pool(tmp_path, defer_writes=deferred) as pool:
        summary = asyncio.run(downloader.download_mirror(
            packages, [mf], pool, unit.name, 2, source=unit.source, track_failures=True))
        assert summary.failed == 4 and summary.metadata_ok
        assert len(pool.retry_plan(unit.name)[1]) == 4
    requests.clear()
    allowed.update(p.url for p in packages[:3])
    with Pool(tmp_path, defer_writes=deferred) as pool:
        monkeypatch.setattr(pool, "ref_map", no_scan)
        monkeypatch.setattr(pool, "mirror_files", no_scan)
        result = repair(pool, unit)
        assert result.downloaded == 3 and result.failed == 1
        assert [f["package"]["url"] for f in pool.retry_plan(unit.name)[1]] == [packages[3].url]
    requests.clear()
    allowed.add(packages[3].url)
    with Pool(tmp_path, defer_writes=deferred) as pool:
        assert repair(pool, unit).downloaded == 1
        assert requests == [packages[3].url]
        assert pool.retry_plan(unit.name)[1] == []
        assert len(pool.ref_map(unit.name)) == 5


def test_cleanup_is_atomic_with_recovered_refs(incomplete, tmp_path):
    _, unit, missing, blocked, upstream = incomplete
    blocked.clear()
    with Pool(tmp_path) as pool:
        before = pool.ref_map(unit.name)
        journal = pool.retry_plan(unit.name)
        pool._conn.executescript("""
            CREATE TEMP TRIGGER fail_cleanup BEFORE DELETE ON main.retry_failures
            BEGIN SELECT RAISE(ABORT, 'cleanup failed'); END;
        """)
        with pytest.raises(sqlite3.IntegrityError, match="cleanup failed"):
            repair(pool, unit)
        assert pool.ref_map(unit.name) == before
        assert pool.retry_plan(unit.name) == journal
        pool._conn.execute("DROP TRIGGER fail_cleanup")
        upstream.requests.clear()
        assert repair(pool, unit).pooled == 1
        assert not upstream.requests
        assert pool.resolve(unit.name, missing.repo_path) is not None
        assert pool.retry_plan(unit.name)[1] == []


def test_normal_sync_replaces_journal_and_metadata_abort_preserves_it(incomplete, tmp_path):
    _, unit, _, blocked, _ = incomplete
    with Pool(tmp_path) as pool, concurrent.futures.ThreadPoolExecutor(2) as ex:
        before = pool.retry_plan(unit.name)
        bad = MetadataFile("", "bad", content=b"bad", expected_sha256="a" * 64)
        result = asyncio.run(downloader.download_mirror(
            [], [bad], pool, unit.name, 2, track_failures=True))
        assert not result.metadata_ok
        assert pool.retry_plan(unit.name) == before
        blocked.clear()
        result = asyncio.run(sync_mirror(unit, pool=pool, mirror_dir=tmp_path,
                                        max_connections=2, executor=ex))
        assert result.ok
        after = pool.retry_plan(unit.name)
        assert after[0] != before[0] and after[1] == []


def test_deep_repair_invalidates_journal_for_changed_pool(incomplete, tmp_path):
    _, unit, missing, blocked, _ = incomplete
    blocked.clear()
    with Pool(tmp_path) as pool:
        assert repair(pool, unit).ok
        checksum = pool.ref_map(unit.name)[missing.repo_path]
        pool.forget_blobs([checksum])
        assert pool.retry_plan(unit.name) is None
        assert repair(pool, unit).downloaded == 1
        assert pool.retry_plan(unit.name)[1] == []


def test_old_database_is_readable_before_migration(incomplete, tmp_path):
    _, unit, _, _, upstream = incomplete
    with sqlite3.connect(tmp_path / ".pool/mmw.db") as conn:
        conn.executescript("DROP TABLE retry_failures; DROP TABLE retry_state;")
    with Pool(tmp_path, read_only=True) as pool:
        assert pool.retry_plan(unit.name) is None
        assert repair(pool, unit, dry_run=True).total == 1
        assert not upstream.requests
    with Pool(tmp_path) as pool:
        assert repair(pool, unit).failed == 1
        assert len(pool.retry_plan(unit.name)[1]) == 1


def test_tui_retry_does_not_scan_pool_totals(incomplete, tmp_path, monkeypatch):
    from mmw.tui.app import MmwApp
    from mmw.tui.ui.widgets import Button
    from waiting import settle, until

    cfg, _, _, blocked, _ = incomplete
    blocked.clear()
    scanned = []

    def forbidden(*args, **kwargs):
        scanned.append(True)
        raise AssertionError("unexpected whole-pool statistics scan")

    monkeypatch.setattr(Pool, "stats", forbidden)
    monkeypatch.setattr(Pool, "ref_map", no_scan)
    monkeypatch.setattr(Pool, "mirror_files", no_scan)

    async def scenario():
        app = MmwApp(cfg, tmp_path, initial_section="sync")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.screen.query_one("#btn-sync-retry", Button).press()
            await until(pilot, lambda: app.active_sync is not None)
            await until(pilot, lambda: not app.active_sync.running and app.active_sync._final_scan_done)
            assert all(not c.has_class("error") for c in app.active_sync._cards.values())
    asyncio.run(scenario())
    assert not scanned


def test_later_success_in_same_sync_does_not_leave_failure(tmp_path, monkeypatch):
    calls = []

    def respond(req):
        calls.append(str(req.url))
        return httpx.Response(404) if len(calls) == 1 else httpx.Response(200, content=b"ok")

    monkeypatch.setattr(downloader, "_thread_client", lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    pkg = PackageInfo("asset", "https://assets.test/a", "", "sha256", 0,
                      "assets.test/a", "a", checksum_known=False)
    with Pool(tmp_path) as pool:
        result = asyncio.run(downloader.download_mirror(
            [pkg, pkg], [], pool, "m", 1, track_failures=True))
        assert result.failed == result.downloaded == 1
        assert pool.retry_plan("m")[1] == []
        assert pool.resolve("m", pkg.repo_path) is not None


def test_normal_commit_failure_rolls_back_journal_and_refs(incomplete, tmp_path):
    _, unit, _, _, _ = incomplete
    with Pool(tmp_path) as pool:
        before_refs = pool.ref_map(unit.name)
        before_queue = pool.retry_plan(unit.name)
        pool._conn.executescript("""
            CREATE TEMP TRIGGER reject_journal BEFORE INSERT ON main.retry_state
            BEGIN SELECT RAISE(ABORT, 'journal failed'); END;
        """)
        with pytest.raises(sqlite3.IntegrityError, match="journal failed"):
            pool.reconcile_refs(unit.name, [], unit.source, [])
        assert pool.ref_map(unit.name) == before_refs
        assert pool.retry_plan(unit.name) == before_queue
