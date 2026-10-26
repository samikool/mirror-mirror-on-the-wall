"""Real downloader/pool behavior with concurrent requests and fake HTTP."""
import asyncio
import concurrent.futures
from dataclasses import replace
import hashlib
import threading
from pathlib import Path

import httpx
import pytest

from mmw.core import downloader as dl
from mmw.core.download_coordinator import DownloadCoordinator
from mmw.core.pool import Pool
from mmw.providers.base import PackageInfo


BODY = b'verified package bytes'
DIGEST = hashlib.sha256(BODY).hexdigest()


def package(name, url, *, known=True):
    return PackageInfo(name, url, DIGEST if known else '', 'sha256', len(BODY),
                       f'pool/{name}', name, checksum_known=known)


class ObservedCoordinator(DownloadCoordinator):
    def __init__(self):
        super().__init__()
        self.requests = 0

    async def run(self, *args, **kwargs):
        self.requests += 1
        return await super().run(*args, **kwargs)


async def until(predicate):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), 10)


def transport(monkeypatch, respond):
    monkeypatch.setattr(dl, '_thread_client', lambda: httpx.Client(transport=httpx.MockTransport(respond)))


async def sync(pool, executor, downloads, sem, name, packages, **kwargs):
    return await dl.download_mirror(packages, [], pool, name, 4, executor=executor,
                                    downloads=downloads, sem=sem, **kwargs)


@pytest.mark.parametrize('known', [True, False], ids=['checksum', 'exact-url'])
def test_parallel_units_download_once_and_keep_each_alias_ref(tmp_path, monkeypatch, known):
    started, release = threading.Event(), threading.Event()
    gets = []
    def respond(request):
        gets.append(str(request.url))
        started.set()
        assert release.wait(10)
        return httpx.Response(200, content=BODY)
    transport(monkeypatch, respond)
    first = package('first.deb', 'https://one.test/package', known=known)
    second = package('second.deb', 'https://two.test/alias' if known else first.url, known=known)

    async def scenario():
        with Pool(tmp_path) as pool, concurrent.futures.ThreadPoolExecutor(4) as executor:
            coordinator = ObservedCoordinator()
            sem = asyncio.Semaphore(1)  # a waiter holding this permit would deadlock the owner
            checks, events, starts, finishes = [], [], [], []
            contains = pool.contains
            def checked(checksum):
                checks.append(checksum)
                return contains(checksum)
            monkeypatch.setattr(pool, 'contains', checked)
            a = asyncio.create_task(sync(pool, executor, coordinator, sem, 'one', [first],
                                        progress=lambda name, result: events.append(result),
                                        start_callback=lambda: starts.append(1), finish_callback=lambda: finishes.append(1)))
            try:
                await until(started.is_set)
                b = asyncio.create_task(sync(pool, executor, coordinator, sem, 'two', [second],
                                            progress=lambda name, result: events.append(result),
                                            start_callback=lambda: starts.append(1), finish_callback=lambda: finishes.append(1)))
                await until(lambda: coordinator.requests == 2)
            finally:
                release.set()
            summaries = await asyncio.wait_for(asyncio.gather(a, b), 10)
            assert all(s.ok for s in summaries)
            assert len(gets) == 1
            assert sum(s.downloaded for s in summaries) == 1
            assert sum(s.pooled for s in summaries) == 1
            assert sum(s.bytes_transferred for s in summaries) == len(BODY)
            assert sorted(r.bytes_transferred for r in events) == [0, len(BODY)]
            assert len(starts) == len(finishes) == 1
            assert checks == ([DIGEST] if known else [])
            assert pool.ref_map('one') == {first.repo_path: DIGEST}
            assert pool.ref_map('two') == {second.repo_path: DIGEST}
            blobs = [p for p in pool._packages_dir.rglob('*') if p.is_file()]
            assert len(blobs) == 1
            assert blobs[0].read_bytes() == BODY
    asyncio.run(scenario())


def test_bad_source_falls_back_to_waiters_url_without_failing_either_unit(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    gets = []
    def respond(request):
        gets.append(str(request.url))
        if request.url.host == 'bad.test':
            started.set()
            assert release.wait(10)
            return httpx.Response(404)
        return httpx.Response(200, content=BODY)
    transport(monkeypatch, respond)
    async def scenario():
        with Pool(tmp_path) as pool, concurrent.futures.ThreadPoolExecutor(4) as executor:
            coordinator, sem = ObservedCoordinator(), asyncio.Semaphore(1)
            starts, finishes = [], []
            a = asyncio.create_task(sync(pool, executor, coordinator, sem, 'one',
                                        [package('a', 'https://bad.test/package')],
                                        start_callback=lambda: starts.append(1), finish_callback=lambda: finishes.append(1)))
            try:
                await until(started.is_set)
                b = asyncio.create_task(sync(pool, executor, coordinator, sem, 'two',
                                            [package('b', 'https://good.test/package')]))
                await until(lambda: coordinator.requests == 2)
            finally:
                release.set()
            summaries = await asyncio.wait_for(asyncio.gather(a, b), 10)
            assert all(s.ok for s in summaries)
            assert gets == ['https://bad.test/package', 'https://good.test/package']
            assert sum(s.downloaded for s in summaries) == 1
            assert sum(s.bytes_transferred for s in summaries) == len(BODY)
            assert len(starts) == len(finishes) == 1
            assert pool.ref_map('one')['pool/a'] == pool.ref_map('two')['pool/b'] == DIGEST
    asyncio.run(scenario())


def test_pool_is_rechecked_after_waiting_for_a_download_slot(tmp_path, monkeypatch):
    gets = []
    transport(monkeypatch, lambda request: gets.append(request) or httpx.Response(200, content=BODY))
    async def scenario():
        with Pool(tmp_path) as pool, concurrent.futures.ThreadPoolExecutor(2) as executor:
            coordinator, sem = ObservedCoordinator(), asyncio.Semaphore(0)
            pkg = package('alias', 'https://example.test/package')
            task = asyncio.create_task(sync(pool, executor, coordinator, sem, 'one', [pkg]))
            await until(lambda: coordinator.requests == 1)
            pool.add(BODY, DIGEST, 'sha256', 'already-arrived', len(BODY))
            sem.release()
            summary = await asyncio.wait_for(task, 10)
            assert summary.ok and summary.pooled == 1 and summary.downloaded == 0
            assert not gets
            assert pool.ref_map('one') == {pkg.repo_path: DIGEST}
    asyncio.run(scenario())


def test_sequential_unknown_url_reuses_computed_hash_but_different_urls_do_not_share(tmp_path, monkeypatch):
    gets = []
    transport(monkeypatch, lambda request: gets.append(str(request.url)) or httpx.Response(200, content=BODY))
    async def scenario():
        with Pool(tmp_path) as pool, concurrent.futures.ThreadPoolExecutor(2) as executor:
            coordinator, sem = DownloadCoordinator(), asyncio.Semaphore(1)
            pkg = package('asset', 'https://one.test/asset', known=False)
            first = await sync(pool, executor, coordinator, sem, 'one', [pkg])
            second = await sync(pool, executor, coordinator, sem, 'two', [replace(pkg, repo_path='alias/asset')])
            third = await sync(pool, executor, coordinator, sem, 'three', [replace(pkg, url='https://two.test/asset')])
            assert all(s.ok for s in [first, second, third])
            assert (first.downloaded, second.pooled, third.downloaded) == (1, 1, 1)
            assert gets == ['https://one.test/asset', 'https://two.test/asset']
            assert pool.ref_map('two') == {'alias/asset': DIGEST}
    asyncio.run(scenario())


def test_failed_claim_does_not_poison_later_retry(tmp_path, monkeypatch):
    gets = []
    transport(monkeypatch, lambda request: gets.append(request) or httpx.Response(404 if len(gets) == 1 else 200, content=BODY))
    async def scenario():
        with Pool(tmp_path) as pool, concurrent.futures.ThreadPoolExecutor(2) as executor:
            coordinator, sem = DownloadCoordinator(), asyncio.Semaphore(1)
            pkg = package('asset', 'https://one.test/asset')
            first = await sync(pool, executor, coordinator, sem, 'one', [pkg])
            second = await sync(pool, executor, coordinator, sem, 'two', [pkg])
            assert first.failed == 1
            assert second.ok and second.downloaded == 1
            assert len(gets) == 2
    asyncio.run(scenario())


def test_cancelling_waiter_does_not_cancel_shared_download(tmp_path):
    async def scenario():
        with Pool(tmp_path) as pool:
            coordinator = DownloadCoordinator()
            started, release = asyncio.Event(), asyncio.Event()
            calls = []
            async def fetch(url, cached_checksum):
                calls.append(url)
                started.set()
                await release.wait()
                return dl.DownloadResult(dl.Status.DOWNLOADED, tmp_path/'asset', len(BODY), checksum=DIGEST)
            pkg = package('asset', 'https://one.test/asset')
            owner = asyncio.create_task(coordinator.run(pool, pkg, fetch))
            await started.wait()
            waiter = asyncio.create_task(coordinator.run(pool, pkg, fetch))
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            release.set()
            assert (await owner).status == dl.Status.DOWNLOADED
            assert calls == [pkg.url]
    asyncio.run(scenario())


def test_cancelling_owner_leaves_shared_job_for_waiter(tmp_path):
    async def scenario():
        with Pool(tmp_path) as pool:
            coordinator = DownloadCoordinator()
            started, release = asyncio.Event(), asyncio.Event()
            calls = []
            async def fetch(url, cached_checksum):
                calls.append(url)
                started.set()
                await release.wait()
                return dl.DownloadResult(dl.Status.DOWNLOADED, tmp_path/'asset', len(BODY), checksum=DIGEST)
            pkg = package('asset', 'https://one.test/asset')
            owner = asyncio.create_task(coordinator.run(pool, pkg, fetch))
            await started.wait()
            waiter = asyncio.create_task(coordinator.run(pool, pkg, fetch))
            await asyncio.sleep(0)
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await owner
            release.set()
            result = await asyncio.wait_for(waiter, 5)
            assert result.status == dl.Status.POOLED and result.bytes_transferred == 0
            assert calls == [pkg.url]
    asyncio.run(scenario())


def test_stopping_shared_download_stops_both_units_without_failures(tmp_path, monkeypatch):
    started, release, stop = threading.Event(), threading.Event(), threading.Event()
    def respond(request):
        started.set()
        assert release.wait(10)
        return httpx.Response(200, content=BODY)
    transport(monkeypatch, respond)
    async def scenario():
        with Pool(tmp_path) as pool, concurrent.futures.ThreadPoolExecutor(4) as executor:
            coordinator, sem = ObservedCoordinator(), asyncio.Semaphore(1)
            pkg = package('asset', 'https://one.test/asset')
            a = asyncio.create_task(sync(pool, executor, coordinator, sem, 'one', [pkg], stop_event=stop))
            try:
                await until(started.is_set)
                b = asyncio.create_task(sync(pool, executor, coordinator, sem, 'two', [pkg], stop_event=stop))
                await until(lambda: coordinator.requests == 2)
                stop.set()
            finally:
                release.set()
            summaries = await asyncio.wait_for(asyncio.gather(a, b), 5)
            await until(lambda: not coordinator._active)
            assert all(s.stopped and not s.failed for s in summaries)
            assert not pool.ref_map('one') and not pool.ref_map('two')
    asyncio.run(scenario())


def test_fallback_checksum_is_verified_before_waiters_can_reuse_it(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    gets, bytes_received = [], []
    def respond(request):
        gets.append(str(request.url))
        if request.url.host == 'bad.test':
            started.set()
            assert release.wait(10)
            return httpx.Response(200, content=b'wrong bytes')
        return httpx.Response(200, content=BODY)
    transport(monkeypatch, respond)
    async def scenario():
        with Pool(tmp_path) as pool, concurrent.futures.ThreadPoolExecutor(4) as executor:
            coordinator, sem = ObservedCoordinator(), asyncio.Semaphore(1)
            a = asyncio.create_task(sync(pool, executor, coordinator, sem, 'one',
                                        [package('a', 'https://bad.test/asset')], chunk_callback=bytes_received.append))
            try:
                await until(started.is_set)
                b = asyncio.create_task(sync(pool, executor, coordinator, sem, 'two',
                                            [package('b', 'https://good.test/asset')]))
                await until(lambda: coordinator.requests == 2)
            finally:
                release.set()
            summaries = await asyncio.wait_for(asyncio.gather(a, b), 10)
            assert all(s.ok for s in summaries)
            assert gets == ['https://bad.test/asset'] * dl._PKG_RETRIES + ['https://good.test/asset']
            assert sum(bytes_received) == len(b'wrong bytes') * dl._PKG_RETRIES + len(BODY)
            assert pool.stats()['packages'] == 1
            assert Path(pool.mirror_files(['one'])[0][1]).read_bytes() == BODY
    asyncio.run(scenario())


def test_registry_does_not_share_content_between_distinct_pools(tmp_path, monkeypatch):
    gets = []
    transport(monkeypatch, lambda request: gets.append(request) or httpx.Response(200, content=BODY))
    async def scenario():
        with Pool(tmp_path/'one') as one, Pool(tmp_path/'two') as two, concurrent.futures.ThreadPoolExecutor(4) as executor:
            coordinator, sem = DownloadCoordinator(), asyncio.Semaphore(2)
            pkg = package('asset', 'https://one.test/asset')
            summaries = await asyncio.gather(sync(one, executor, coordinator, sem, 'one', [pkg]),
                                             sync(two, executor, coordinator, sem, 'two', [pkg]))
            assert all(s.ok and s.downloaded == 1 for s in summaries)
            assert len(gets) == 2
            assert one.contains(DIGEST) and two.contains(DIGEST)
    asyncio.run(scenario())


def test_headless_sync_shares_unknown_url_cache_across_units(tmp_path, monkeypatch):
    from mmw.commands import mirror as command
    from mmw.core import config, sync_engine
    gets = []
    transport(monkeypatch, lambda request: gets.append(request) or httpx.Response(200, content=BODY))
    units = [config.MirrorConfig(name=name, provider='apt', source='https://one.test/apt',
                                source_url='https://one.test/apt') for name in ['one', 'two']]
    monkeypatch.setattr(command, 'resolve_and_report', lambda cfg: (units, []))
    monkeypatch.setattr(sync_engine, 'fetch_all_threaded', lambda provider, unit, **kwargs: (
        [replace(package('asset', 'https://one.test/asset', known=False), repo_path=unit.name+'/asset')], []))
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path/'mirror'))
    assert asyncio.run(command._mirror_all(cfg)) == 0
    assert len(gets) == 1
    with Pool(tmp_path/'mirror', read_only=True) as pool:
        assert pool.ref_map('one') == {'one/asset': DIGEST}
        assert pool.ref_map('two') == {'two/asset': DIGEST}


def test_largest_files_download_first(tmp_path, monkeypatch):
    """Big files start first: a slow (uncached-on-the-CDN) one overlaps the
    many small ones instead of running alone at the end of a sync."""
    bodies = {"small": b"s" * 10, "big": b"b" * 5000, "mid": b"m" * 700}
    asked = []

    def respond(request):
        name = request.url.path.rsplit("/", 1)[-1]
        asked.append(name)
        return httpx.Response(200, content=bodies[name])
    transport(monkeypatch, respond)
    pkgs = [PackageInfo(n, f"https://example.com/{n}", hashlib.sha256(b).hexdigest(), "sha256", len(b),
                        f"pool/{n}", n) for n, b in bodies.items()]

    async def scenario():
        with Pool(tmp_path / "pool") as pool, concurrent.futures.ThreadPoolExecutor(1) as executor:
            await dl.download_mirror(pkgs, [], pool, "m", 1, executor=executor, sem=asyncio.Semaphore(1))
    asyncio.run(scenario())
    assert asked == ["big", "mid", "small"]
