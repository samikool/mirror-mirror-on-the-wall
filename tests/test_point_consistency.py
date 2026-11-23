"""A repository point must describe one readable APT suite."""
import asyncio
import concurrent.futures
import hashlib

import pytest

from apt_upstream import UpstreamRepo
from debs import make_deb
from mmw.core import config, points
from mmw.core.point_check import check_suite, newest_release
from mmw.core.pool import Pool
from mmw.core.repository_points import current_repository_points
from mmw.core.sync_engine import sync_mirror


def _sync(unit, folder, releases=None):
    with Pool(folder) as pool, concurrent.futures.ThreadPoolExecutor(2) as ex:
        result = asyncio.run(sync_mirror(unit, pool=pool, mirror_dir=folder,
                                         max_connections=2, executor=ex, releases=releases))
        assert result.ok, (result.errors, result.metadata_errors)


@pytest.mark.parametrize('new_component,old_component', [('main', 'universe'), ('universe', 'main')])
def test_a_suite_synced_across_a_republish_keeps_its_previous_point(tmp_path, new_component, old_component):
    debs = {('jammy', c): [make_deb(tmp_path / 'old', p, '1')]
            for c, p in [('main', 'alpha'), ('universe', 'beta')]}
    up = UpstreamRepo(tmp_path, debs)
    try:
        src = up.source(['jammy'], ('main', 'universe'))
        md = tmp_path / 'mirror'
        cfg = config.Config(settings=config.Settings(mirror_dir=md), sources=[src])
        units = src.to_mirrors()
        for unit in units:
            _sync(unit, md)
        points.record_points_after_sync(cfg, md, {units[0].source})
        with Pool(md, read_only=True) as pool:
            previous = current_repository_points(pool, units[0].source)
        assert previous
        up.republish({('jammy', c): [make_deb(tmp_path / 'new', p, '2')]
                      for c, p in [('main', 'alpha'), ('universe', 'beta')]},
                     date='Fri, 22 Apr 2022 10:00:00 UTC')
        _sync(next(u for u in units if u.component == new_component), md)
        said = []
        points.record_points_after_sync(cfg, md, {units[0].source}, say=said.append)
        assert any(f'{old_component} synced against an older Release — sync it again' in s for s in said), said
        with Pool(md, read_only=True) as pool:
            assert current_repository_points(pool, units[0].source) == previous
            for pid in previous:
                files = pool.point_files(pid)
                assert check_suite(files, pool.track_of_point(pid).name, points._reader(pool, files)) == []
    finally:
        up.close()


def test_newest_release_wins_by_date():
    blobs = {'z': b'Date: Thu, 21 Apr 2022 17:16:08 UTC\n',
             'a': b'Date: Fri, 22 Apr 2022 10:00:00 UTC\n'}
    assert newest_release({'Release': ['z', 'a']}, blobs.get) == 'a'


def test_one_point_one_date(tmp_path):
    up = UpstreamRepo(tmp_path, {('jammy', 'main'): [make_deb(tmp_path / 'old', 'alpha', '1')]})
    try:
        src = up.source(['jammy'])
        md = tmp_path / 'mirror'
        cfg = config.Config(settings=config.Settings(mirror_dir=md), sources=[src],
                            networks=[config.NetworkConfig(name='lab', root=tmp_path / 'pub')])
        [unit] = src.to_mirrors()
        for day in (21, 22):
            up.republish({('jammy', 'main'): [make_deb(tmp_path / str(day), 'alpha', str(day))]},
                         date=f'Fri, {day} Apr 2022 10:00:00 UTC')
            _sync(unit, md)
            said = []
            points.record_points_after_sync(cfg, md, {unit.source}, say=said.append)
            repo = next(s for s in said if s.startswith('New repository point(s)'))
            network = next(s for s in said if s.startswith('lab: new point(s)'))
            assert f'2022-04-{day}' in repo and f'2022-04-{day}' in network
    finally:
        up.close()


def test_check_suite_names_missing_packages_and_mismatched_indexes():
    index = b'Package: alpha\nFilename: pool/alpha.deb\nSHA256: abc\n\n'
    checksum = hashlib.sha256(index).hexdigest()
    release = f'SHA256:\n {checksum} {len(index)} main/binary-amd64/Packages\n'.encode()
    files = {'h/dists/jammy/Release': 'release', 'h/dists/jammy/main/binary-amd64/Packages': 'wrong'}
    blobs = {'h/dists/jammy/Release': release, 'h/dists/jammy/main/binary-amd64/Packages': index}
    problems = check_suite(files, 'h/dists/jammy', blobs.get)
    assert any('does not match its Release' in p for p in problems)
    assert any('pool/alpha.deb' in p for p in problems)
    files['h/dists/jammy/main/binary-amd64/by-hash/SHA256/' + checksum] = checksum
    assert not any('does not match its Release' in p for p in check_suite(files, 'h/dists/jammy', blobs.get))


def test_refresh_during_a_shared_run_keeps_the_previous_point(tmp_path):
    from mmw.core.release_snapshots import ReleaseSnapshots
    a = make_deb(tmp_path / 'old', 'alpha', '1')
    b = make_deb(tmp_path / 'old', 'beta', '1')
    up = UpstreamRepo(tmp_path, {('jammy', 'main'): [a], ('jammy', 'universe'): [b]})
    try:
        src = up.source(['jammy'], ('main', 'universe'))
        md = tmp_path / 'mirror'
        cfg = config.Config(settings=config.Settings(mirror_dir=md), sources=[src])
        main, universe = src.to_mirrors()
        for unit in (main, universe):
            _sync(unit, md)
        points.record_points_after_sync(cfg, md, {main.source})
        with Pool(md, read_only=True) as pool:
            previous = current_repository_points(pool, main.source)
        releases = ReleaseSnapshots()
        _sync(main, md, releases)
        a2 = make_deb(tmp_path / 'new', 'alpha', '2')
        b2 = make_deb(tmp_path / 'new', 'beta', '2')
        up.republish({('jammy', 'main'): [a2], ('jammy', 'universe'): [b2]},
                     date='Fri, 22 Apr 2022 10:00:00 UTC')
        _sync(universe, md, releases)
        said = []
        points.record_points_after_sync(cfg, md, {main.source}, say=said.append)
        assert any('main synced against an older Release' in s for s in said)
        with Pool(md, read_only=True) as pool:
            assert current_repository_points(pool, main.source) == previous
    finally:
        up.close()


def test_projection_keeps_trust_roots_and_signature_from_one_release():
    from mmw.core.repository_points import _project
    blobs = {'old': b'Date: Thu, 21 Apr 2022 17:16:08 UTC\n',
             'new': b'Date: Fri, 22 Apr 2022 10:00:00 UTC\n'}
    root = 'h/dists/jammy/'
    old = {root + 'Release': ('old', 'release', False),
           root + 'Release.gpg': ('z-old-signature', 'release', False)}
    new = {root + 'Release': ('new', 'release', False),
           root + 'Release.gpg': ('a-new-signature', 'release', False)}
    rows = [(p, cs, k, h, False) for group in (old, new) for p, (cs, k, h) in group.items()]
    assert _project(rows, blobs.get, [old, new])[root + 'Release.gpg'] == 'a-new-signature'
    # The newer upstream no longer serves a detached Release at all.
    new = {root + 'InRelease': ('new', 'release', False)}
    rows = [(p, cs, k, h, False) for group in (old, new) for p, (cs, k, h) in group.items()]
    projected = _project(rows, blobs.get, [old, new])
    assert projected == {root + 'InRelease': 'new'}
