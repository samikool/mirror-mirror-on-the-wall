"""One upstream trust root shared by every component in a sync run."""
import asyncio
import concurrent.futures
import hashlib
from collections import Counter

import httpx
import pytest

from apt_upstream import UpstreamRepo
from debs import make_deb
from mmw.core.pool import Pool
from mmw.core.release_snapshots import ReleaseSnapshots
from mmw.core.sync_engine import sync_mirror
from mmw.providers.apt import AptProvider, _parse_release_files


@pytest.fixture
def upstream(tmp_path):
    a = make_deb(tmp_path / 'debs', 'alpha', '1')
    b = make_deb(tmp_path / 'debs', 'beta', '1')
    repo = UpstreamRepo(tmp_path, {('jammy', 'main'): [a], ('jammy', 'universe'): [b]})
    try:
        yield repo, a, b
    finally:
        repo.close()


def republish(upstream, tmp_path, *, changed=False):
    up, a, b = upstream
    if changed:
        b = make_deb(tmp_path / 'new', 'beta', '2')
    up.republish({('jammy', 'main'): [a], ('jammy', 'universe'): [b]},
                 date='Fri, 22 Apr 2022 10:00:00 UTC')


def sync(unit, folder, releases):
    with Pool(folder) as pool, concurrent.futures.ThreadPoolExecutor(2) as ex:
        result = asyncio.run(sync_mirror(unit, pool=pool, mirror_dir=folder,
                                         max_connections=2, executor=ex, releases=releases))
        assert result.ok, (result.errors, result.metadata_errors)
        return pool.ref_map(unit.name)


def test_units_of_one_suite_share_one_release(upstream, tmp_path):
    up, _, _ = upstream
    main, universe = up.source(['jammy'], ('main', 'universe')).to_mirrors()
    releases = ReleaseSnapshots()
    first = sync(main, tmp_path / 'mirror', releases)
    republish(upstream, tmp_path)
    second = sync(universe, tmp_path / 'mirror', releases)
    assert first['dists/jammy/InRelease'] == second['dists/jammy/InRelease']
    with Pool(tmp_path / 'mirror', read_only=True) as pool:
        release = pool.blob_file(second['dists/jammy/InRelease']).read_text()
        for cs, path, _ in _parse_release_files(release):
            if 'dists/jammy/' + path in second:
                assert second['dists/jammy/' + path] == cs


def test_a_new_run_sees_the_republish(upstream, tmp_path):
    up, _, _ = upstream
    with httpx.Client() as client:
        first = ReleaseSnapshots().get(client, up.url + '/dists/jammy')
        republish(upstream, tmp_path)
        second = ReleaseSnapshots().get(client, up.url + '/dists/jammy')
    assert first[0].content != second[0].content


def test_refresh_after_by_hash_gc_refetches_once(upstream, tmp_path):
    up, _, _ = upstream
    main, universe = up.source(['jammy'], ('main', 'universe')).to_mirrors()
    releases, gets = ReleaseSnapshots(), Counter()
    with httpx.Client(event_hooks={'request': [lambda req: gets.update([req.url.path])]}) as client:
        AptProvider().fetch_all(main, client, releases=releases)
        republish(upstream, tmp_path, changed=True)
        packages, files = AptProvider().fetch_all(universe, client, releases=releases)
        assert packages[0].name == 'beta'
        assert '_2_' in packages[0].filename
        index = next(f for f in files if f.repo_path.endswith('/Packages.xz'))
        assert hashlib.sha256(index.content).hexdigest() == index.expected_sha256
        assert gets['/ubuntu/dists/jammy/InRelease'] == 2
        seen = next(f.content for f in files if f.repo_path.endswith('/InRelease'))
        releases.refresh(client, up.url + '/dists/jammy', seen)
        assert gets['/ubuntu/dists/jammy/InRelease'] == 2


def test_get_is_thread_safe_and_returns_independent_files():
    gets = Counter()

    def respond(request):
        gets.update([request.url.path])
        return httpx.Response(200, content=b'Date: Thu, 21 Apr 2022 17:16:08 UTC\n')

    releases = ReleaseSnapshots()
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with concurrent.futures.ThreadPoolExecutor(8) as ex:
            results = list(ex.map(lambda _: releases.get(client, 'https://test/dists/jammy'), range(20)))
    assert gets['/dists/jammy/InRelease'] == 1
    results[0][0].content = b'changed'
    assert results[1][0].content != b'changed'


def test_metadata_retry_refreshes_the_shared_release(upstream, tmp_path, monkeypatch):
    from mmw.core import sync_engine
    from mmw.core.downloader import MirrorSummary
    up, _, _ = upstream
    unit = up.source(['jammy']).to_mirrors()[0]
    releases = ReleaseSnapshots()
    old = None
    with httpx.Client() as client:
        old = releases.get(client, up.url + '/dists/jammy')[0].content

    async def download(**kwargs):
        republish(upstream, tmp_path)
        files = kwargs['refetch_metadata']()
        assert next(f.content for f in files if f.repo_path.endswith('/InRelease')) != old
        return MirrorSummary(unit.name)

    monkeypatch.setattr(sync_engine, 'download_mirror', download)
    sync(unit, tmp_path / 'mirror', releases)


def test_concurrent_refreshes_compare_the_seen_release():
    gets = Counter()
    generation = [b'old']

    def respond(request):
        gets.update([request.url.path])
        return httpx.Response(200, content=generation[0])

    releases = ReleaseSnapshots()
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        releases.get(client, 'https://test/dists/jammy')
        generation[0] = b'new'
        with concurrent.futures.ThreadPoolExecutor(8) as ex:
            results = list(ex.map(lambda _: releases.refresh(client, 'https://test/dists/jammy', b'old'), range(20)))
    assert gets['/dists/jammy/InRelease'] == 2
    assert all(files[0].content == b'new' for files in results)
