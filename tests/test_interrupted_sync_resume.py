"""Completed checksumless downloads survive an interrupted unit/process."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import sqlite3
import subprocess
import sys
import threading

import httpx
import pytest

from mmw.core import downloader as dl, sync_engine
from mmw.core.config import MirrorConfig
from mmw.core.pool import Pool
from mmw.providers.base import MetadataFile, PackageInfo


BODY = b'completed extension asset'
DIGEST = hashlib.sha256(BODY).hexdigest()
URL = 'https://example.test/extensions/publisher/name/1.0/asset'


def asset():
    return PackageInfo('extension asset', URL, '', 'sha256', len(BODY),
                       'extensions/1.0/asset', 'asset', checksum_known=False,
                       item_id='publisher.name')


def fake_http(monkeypatch):
    requests = []
    def response(request):
        requests.append(str(request.url))
        return httpx.Response(200, content=BODY)
    monkeypatch.setattr(dl, '_thread_client', lambda: httpx.Client(transport=httpx.MockTransport(response)))
    return requests


@pytest.mark.parametrize('defer', [False, True], ids=['inline', 'deferred'])
@pytest.mark.parametrize('failure', ['stopped', 'metadata-failed'])
def test_restart_reuses_completed_assets_without_publishing_partial_refs(tmp_path, monkeypatch, defer, failure):
    requests = fake_http(monkeypatch)
    previous = hashlib.sha256(b'previous').hexdigest()
    with Pool(tmp_path, defer_writes=defer) as pool:
        pool.add(b'previous', previous, 'sha256', 'previous', 8)
        pool.reconcile_refs('unit', [(previous, 'previous', 'pkg')])
        stop = threading.Event()
        metadata = [MetadataFile('unused', 'index', content=b'invalid', expected_sha256='0' * 64)]
        summary = asyncio.run(dl.download_mirror(
            [asset()], metadata if failure == 'metadata-failed' else [], pool, 'unit', 1,
            stop_event=stop, progress=(lambda *_: stop.set()) if failure == 'stopped' else None))
        assert not summary.ok
        assert summary.downloaded == 1
        assert pool.ref_map('unit') == {'previous': previous}
    # New Pool and coordinator: no process-local cache or finalized asset refs.
    with Pool(tmp_path, defer_writes=defer) as pool:
        assert pool.download_checksums([(URL, 'sha256')]) == {(URL, 'sha256'): DIGEST}
        results = []
        summary = asyncio.run(dl.download_mirror(
            [asset()], [], pool, 'unit', 1, progress=lambda _, result: results.append(result)))
        assert summary.ok and summary.pooled == 1 and summary.downloaded == 0
        assert summary.bytes_transferred == 0
        assert results[0].status == dl.Status.POOLED
        assert pool.ref_map('unit') == {asset().repo_path: DIGEST}
    assert requests == [URL]


def test_mapping_survives_process_exit_without_final_reconcile(tmp_path, monkeypatch):
    script = '''
import hashlib, os, sys
from pathlib import Path
from mmw.core.pool import Pool
pool = Pool(Path(sys.argv[1]))
body = b'completed extension asset'
pool.add(body, hashlib.sha256(body).hexdigest(), 'sha256', 'asset', len(body), source_url=sys.argv[2])
assert not pool.flush_writes()
os._exit(0)
'''
    subprocess.run([sys.executable, '-c', script, str(tmp_path), URL], check=True, timeout=15)
    requests = fake_http(monkeypatch)
    with Pool(tmp_path) as pool:
        assert pool.ref_map('unit') == {}
        summary = asyncio.run(dl.download_mirror([asset()], [], pool, 'unit', 1))
        assert summary.ok and summary.pooled == 1 and summary.bytes_transferred == 0
    assert not requests


def test_dry_run_counts_interrupted_assets_as_pooled(tmp_path, monkeypatch):
    with Pool(tmp_path) as pool:
        pool.add(BODY, DIGEST, 'sha256', 'asset', len(BODY), source_url=URL)
    monkeypatch.setattr(sync_engine, 'fetch_packages_threaded', lambda *_: [asset()])
    unit = MirrorConfig(name='unit', source_url='https://example.test/repo', provider='apt')
    with Pool(tmp_path, read_only=True) as pool, ThreadPoolExecutor(2) as executor:
        stats = asyncio.run(sync_engine.classify_mirror(unit, pool=pool, executor=executor))
        assert (stats.total, stats.in_pool, stats.to_fetch, stats.fetch_size) == (1, 1, 0, 0)


def test_source_mapping_can_resume_into_another_units_alias(tmp_path, monkeypatch):
    with Pool(tmp_path) as pool:
        pool.add(BODY, DIGEST, 'sha256', 'asset', len(BODY), source_url=URL)
    requests = fake_http(monkeypatch)
    with Pool(tmp_path) as pool:
        pkg = replace(asset(), repo_path='other/asset', filename='alias')
        summary = asyncio.run(dl.download_mirror([pkg], [], pool, 'another-unit', 1))
        assert summary.ok and summary.pooled == 1
        assert pool.ref_map('another-unit') == {'other/asset': DIGEST}
    assert not requests


def test_pruned_mapping_does_not_skip_download(tmp_path, monkeypatch):
    with Pool(tmp_path, defer_writes=False) as pool:
        pool.add(BODY, DIGEST, 'sha256', 'asset', len(BODY), source_url=URL)
        pool._conn.execute('DELETE FROM packages WHERE checksum=?', (DIGEST,))
        pool._conn.commit()
        assert pool.download_checksums([(URL, 'sha256')]) == {}
    requests = fake_http(monkeypatch)
    with Pool(tmp_path) as pool:
        summary = asyncio.run(dl.download_mirror([asset()], [], pool, 'unit', 1))
        assert summary.ok and summary.downloaded == 1
    assert requests == [URL]


def test_read_only_old_pool_needs_no_migration_then_writable_open_upgrades(tmp_path):
    with Pool(tmp_path):
        pass
    with sqlite3.connect(tmp_path / '.pool/mmw.db') as conn:
        conn.execute('DROP TABLE download_sources')
    with Pool(tmp_path, read_only=True) as pool:
        assert pool.download_checksums([(URL, 'sha256')]) == {}
        assert not pool._has_table('download_sources')
    with Pool(tmp_path) as pool:
        pool.add(BODY, DIGEST, 'sha256', 'asset', len(BODY), source_url=URL)
    with Pool(tmp_path, read_only=True) as pool:
        assert pool.download_checksums([(URL, 'sha256')]) == {(URL, 'sha256'): DIGEST}


def test_source_lookup_batches_large_lists_and_separates_hash_algorithms(tmp_path):
    urls = [f'{URL}/{index}' for index in range(1100)]
    with Pool(tmp_path) as pool:
        for url in urls:
            pool.add(BODY, DIGEST, 'sha256', 'asset', len(BODY), source_url=url)
        pool.flush_writes()
        found = pool.download_checksums([(url, 'SHA256') for url in urls] + [(URL, 'sha512')])
        assert found == {(url, 'sha256'): DIGEST for url in urls}


def test_pending_source_is_visible_without_flushing_download_thread(tmp_path, monkeypatch):
    from mmw.core.pool import _DeferredWriter
    monkeypatch.setattr(_DeferredWriter, '_commit', lambda *_: None)
    with Pool(tmp_path) as pool:
        pool.add(BODY, DIGEST, 'sha256', 'asset', len(BODY), source_url=URL)
        assert pool.download_checksums([(URL, 'sha256')]) == {(URL, 'sha256'): DIGEST}


@pytest.mark.parametrize('defer', [False, True], ids=['inline', 'deferred'])
def test_blob_and_source_mapping_commit_atomically_on_write_failure(tmp_path, defer):
    with Pool(tmp_path, defer_writes=defer) as pool:
        pool._conn.execute("CREATE TRIGGER refuse_mapping BEFORE INSERT ON download_sources "
                           "BEGIN SELECT RAISE(ABORT, 'mapping write failed'); END")
        pool._conn.commit()
        if defer:
            pool.add(BODY, DIGEST, 'sha256', 'asset', len(BODY), source_url=URL)
            errors = pool.flush_writes()
            assert any('mapping write failed' in error for error in errors)
        else:
            with pytest.raises(sqlite3.IntegrityError, match='mapping write failed'):
                pool.add(BODY, DIGEST, 'sha256', 'asset', len(BODY), source_url=URL)
            assert not pool._conn.in_transaction
        assert not pool.contains(DIGEST)
        assert pool.download_checksums([(URL, 'sha256')]) == {}


def test_apt_style_known_checksum_restart_needs_no_source_mapping(tmp_path, monkeypatch):
    requests = fake_http(monkeypatch)
    pkg = replace(asset(), checksum=DIGEST, checksum_known=True)
    stop = threading.Event()
    with Pool(tmp_path) as pool:
        summary = asyncio.run(dl.download_mirror([pkg], [], pool, 'apt-unit', 1,
                                                stop_event=stop, progress=lambda *_: stop.set()))
        assert summary.stopped and summary.downloaded == 1
    with Pool(tmp_path) as pool:
        assert pool.download_checksums([(URL, 'sha256')]) == {}
        summary = asyncio.run(dl.download_mirror([pkg], [], pool, 'apt-unit', 1))
        assert summary.ok and summary.pooled == 1 and summary.bytes_transferred == 0
    assert requests == [URL]
