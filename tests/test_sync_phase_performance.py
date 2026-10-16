"""Sync scales with distinct lookups and metadata batches, preserving its gates."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading
import time

import pytest

from mmw.core import downloader as dl
from mmw.core.pool import KIND_META, KIND_PKG, Pool
from mmw.providers.base import MetadataFile, PackageInfo


def _add(pool, body, name, **kw):
    cs = hashlib.sha256(body).hexdigest()
    pool.add(body, cs, "sha256", name, len(body), **kw)
    return cs


def _package(path):
    return PackageInfo(path, f"https://x/{path}", "", "sha256", 0, path, path, checksum_known=False)


def test_url_identities_avoid_loading_previous_refs(tmp_path, monkeypatch):
    pkg = _package("extension")
    with Pool(tmp_path) as pool:
        cs = _add(pool, b"asset", "asset", source_url=pkg.url)
        # Both queued and committed URL identities must use the fast path.
        def unexpected_refs(*_):
            pytest.fail("a known URL caused a full previous-ref lookup")
        monkeypatch.setattr(pool, "ref_map", unexpected_refs)
        for _ in range(2):
            assert dl.lookup_packages([pkg], pool, "unit") == (set(), {}, {(pkg.url, "sha256"): cs})
            pool.flush_writes()


def test_mixed_lookup_preserves_url_priority_and_legacy_fallback(tmp_path, monkeypatch):
    with Pool(tmp_path) as pool:
        old = _add(pool, b"old", "old")
        new = _add(pool, b"new", "new", source_url="https://x/matched")
        legacy = _add(pool, b"legacy", "legacy")
        metadata = _add(pool, b"metadata", "metadata")
        pool.reconcile_refs("unit", [(old, "matched", KIND_PKG), (legacy, "legacy", KIND_PKG),
                                     ("f" * 64, "missing", KIND_PKG), (metadata, "index", KIND_META)])
        batches = []
        original = pool.batch_contains
        def batch(checksums):
            batches.append(set(checksums))
            return original(checksums)
        monkeypatch.setattr(pool, "batch_contains", batch)
        held, previous, sources = dl.lookup_packages(
            [_package(p) for p in ("matched", "legacy", "missing")], pool, "unit")
        assert held == set()
        assert previous == {"legacy": legacy}
        assert sources == {("https://x/matched", "sha256"): new}
        assert batches == [set(), {legacy, "f" * 64}]


def test_checksum_batches_deduplicate_shared_blobs(tmp_path):
    with Pool(tmp_path) as pool:
        cs = _add(pool, b"asset", "asset")
        pool.flush_writes()
        queries = []
        with pool._read() as conn:
            conn.set_trace_callback(queries.append)
        assert pool.batch_contains([cs, "f" * 64] * 10000) == {cs}
        assert len([q for q in queries if q.startswith("SELECT")]) == 1


def test_checksum_merge_keeps_all_entries_across_partial_runs(tmp_path):
    with Pool(tmp_path, defer_writes=False) as pool:
        checksums = [_add(pool, str(i).encode(), f"{i}.asset") for i in range(1003)]
        requested = list(reversed(checksums)) + checksums[::7] + ["f" * 64]
        assert pool.batch_contains(requested) == set(checksums)


class _RecordingExecutor(ThreadPoolExecutor):
    def __init__(self):
        super().__init__(max_workers=4)
        self.submissions = 0

    def submit(self, *a, **kw):
        self.submissions += 1
        return super().submit(*a, **kw)


@pytest.mark.parametrize("cached", [False, True])
def test_prefetched_metadata_uses_one_worker_batch(cached):
    body = b"{}"
    digest = hashlib.sha256(body).hexdigest()
    files = [MetadataFile("unused", f"{i}.json", content=body, expected_sha256=digest) for i in range(1000)]
    with _RecordingExecutor() as executor:
        results = asyncio.run(dl._verify_metadata_batch(files, {digest} if cached else set(), executor, None, 4))
    assert executor.submissions == (0 if cached else 1)
    assert all(r[0] is (dl.Fetch.ALREADY_DONE if cached else dl.Fetch.OK) for r in results)


def test_remote_metadata_limits_inflight_work(monkeypatch):
    active = peak = 0
    lock = threading.Lock()
    def fetch(*_):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.002)
        with lock:
            active -= 1
        return dl.Fetch.OK, b"{}", None
    monkeypatch.setattr(dl, "_fetch_or_skip", fetch)
    files = [MetadataFile("unused", f"{i}.json", expected_sha256="f" * 64) for i in range(100)]
    with ThreadPoolExecutor(8) as executor:
        results = asyncio.run(dl._verify_metadata_batch(files, set(), executor, None, 2))
    assert peak <= 2
    assert len(results) == len(files) and all(r[0] is dl.Fetch.OK for r in results)


def test_large_prefetched_indexes_keep_separate_worker_jobs():
    body = b"x" * (9 << 20)
    digest = hashlib.sha256(body).hexdigest()
    files = [MetadataFile("unused", f"{i}.gz", content=body, expected_sha256=digest) for i in range(2)]
    with _RecordingExecutor() as executor:
        results = asyncio.run(dl._verify_metadata_batch(files, set(), executor, None, 4))
    assert executor.submissions == 2
    assert all(r[0] is dl.Fetch.OK for r in results)


def test_bad_prefetched_metadata_aborts_entire_batch(tmp_path):
    good = b"good"
    digest = hashlib.sha256(good).hexdigest()
    with Pool(tmp_path) as pool:
        old = _add(pool, b"old", "old")
        pool.reconcile_refs("unit", [(old, "previous", KIND_META)])
        metadata = [MetadataFile("unused", "good.json", content=good, expected_sha256=digest),
                    MetadataFile("unused", "bad.json", content=b"bad", expected_sha256=digest)]
        result = asyncio.run(dl.download_mirror([], metadata, pool, "unit", 2))
        assert not result.ok and not result.metadata_ok
        assert pool.ref_map("unit") == {"previous": old}
        assert not pool.contains(digest), "part of an invalid batch was pooled"


def test_stopping_during_metadata_writes_keeps_previous_refs(tmp_path, monkeypatch):
    stop = threading.Event()
    with Pool(tmp_path) as pool:
        old = _add(pool, b"old", "old")
        pool.reconcile_refs("unit", [(old, "previous", KIND_META)])
        original = pool.add
        def add(*a, **kw):
            path = original(*a, **kw)
            stop.set()
            return path
        monkeypatch.setattr(pool, "add", add)
        metadata = [MetadataFile("unused", f"{i}.json", content=str(i).encode(),
                                 expected_sha256=hashlib.sha256(str(i).encode()).hexdigest()) for i in range(3)]
        result = asyncio.run(dl.download_mirror([], metadata, pool, "unit", 2, stop_event=stop))
        assert result.stopped and not result.metadata_ok
        assert pool.ref_map("unit") == {"previous": old}
