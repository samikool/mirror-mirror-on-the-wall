"""Repository history is stored once; networks select unit memberships."""
import sqlite3

from mmw.core.pool import Pool
from mmw.site.tracks import PointDraft, content_ident


def draft():
    return PointDraft('h/dists/stable', 'release:one', '20261101T000000Z',
                      {'Suite': 'stable'}, {'h/dists/stable/Release': 'release',
                                           'h/pool/a.deb': 'a', 'h/pool/b.deb': 'b'})


def test_membership_slices_by_dims_and_shared_files(tmp_path):
    from mmw.core.repository_points import slice_files
    with Pool(tmp_path) as pool:
        pid, _ = pool.record_point('R', draft(), 'now')
        pool.record_point_units(pid, 'R', [
            ({}, ['h/dists/stable/Release']),
            ({'arch': 'amd64', 'suite': 'stable'}, ['h/pool/a.deb']),
            ({'suite': 'stable', 'arch': 'arm64'}, ['h/pool/b.deb']),
        ])
        assert slice_files(pool, pid, [('R', {'suite': 'stable', 'arch': 'amd64'})]) == {
            'h/dists/stable/Release': 'release', 'h/pool/a.deb': 'a'}
        assert slice_files(pool, pid, [('R', {'arch': 'arm64', 'suite': 'stable'})]) == {
            'h/dists/stable/Release': 'release', 'h/pool/b.deb': 'b'}
        assert len(pool.repository_points('R')) == 1


def test_migration_preserves_legacy_and_allows_equal_repository_point(tmp_path):
    with Pool(tmp_path) as pool:
        old, _ = pool.record_point('R', draft(), 'old')
        pool.link_network_point('oak', old, 'old')
    db = sqlite3.connect(tmp_path / '.pool/mmw.db')
    db.execute('DELETE FROM repo_points_migrated')
    db.commit()
    db.close()
    with Pool(tmp_path) as pool:
        assert pool.points_of([old])[old].legacy
        assert pool.network_slice('oak', old) == content_ident(draft().files)
        new, created = pool.record_point('R', draft(), 'new')
        assert created and new != old
        assert [p.id for p in pool.repository_points('R')] == [new]
    with Pool(tmp_path) as pool:
        assert not pool.points_of([new])[new].legacy
        assert pool.points_of([old])[old].legacy


def test_sent_slice_is_distinct_from_current_slice(tmp_path):
    with Pool(tmp_path) as pool:
        pid, _ = pool.record_point('R', draft(), 'now')
        pool.set_network_slice('oak', pid, 'narrow')
        pool.record_sent('oak', [pid], [], 'sent')
        assert pool.sent_slice('oak', pid) == 'narrow'
        pool.set_network_slice('oak', pid, 'wide')
        assert pool.sent_slice('oak', pid) == 'narrow'
        pool.record_sent('oak', [pid], [], 'again')
        assert pool.sent_slice('oak', pid) == 'wide'


def test_pre_upgrade_schema_is_migrated_without_changing_ids(tmp_path):
    """Exercise the actual old uniqueness constraint, rather than only the marker."""
    with Pool(tmp_path) as pool:
        pid, _ = pool.record_point('R', draft(), 'old')
        pool.link_network_point('oak', pid, 'old')
        pool.record_sent('oak', [pid], [], 'sent')
    db = sqlite3.connect(tmp_path / '.pool/mmw.db')
    db.executescript('''
        CREATE TABLE old_points (
            id INTEGER PRIMARY KEY, track INTEGER NOT NULL, ident TEXT NOT NULL,
            files TEXT NOT NULL, date TEXT NOT NULL, stamp TEXT NOT NULL,
            held INTEGER NOT NULL DEFAULT 1, UNIQUE(track, ident, files));
        INSERT INTO old_points SELECT id, track, ident, files, date, stamp, held FROM points;
        DROP TABLE points;
        ALTER TABLE old_points RENAME TO points;
        DROP TABLE repo_points_migrated;
        DROP TABLE network_slices;
        DROP TABLE sent_slices;
    ''')
    db.close()
    with Pool(tmp_path, read_only=True) as pool:
        assert pool.points_of([pid])[pid].legacy
        assert pool.repository_points('R') == []
    with Pool(tmp_path) as pool:
        assert pool.points_of([pid])[pid].legacy
        assert pool.sent_slice('oak', pid) == content_ident(draft().files)
        assert pool.record_point('R', draft(), 'new')[0] != pid
        assert pool.network_points('oak')[0].id == pid


def apt_repository(tmp_path):
    import hashlib
    from mmw.core import config
    from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE
    src = config.SourceConfig(url='https://apt.test/debian', suites=['stable'],
                              components=['main'], arches=['amd64', 'arm64'])
    md = tmp_path / 'mirror'
    units = src.to_mirrors()
    release = b'Suite: stable\nDate: Sun, 01 Nov 2026 00:00:00 UTC\n'
    with Pool(md) as pool:
        for unit in units:
            files = [(f'pool/{unit.arch}.deb', unit.arch.encode(), KIND_PKG),
                     ('pool/shared.deb', b'shared', KIND_PKG),
                     ('dists/stable/Release', release, KIND_RELEASE)]
            index = ''.join(f'Package: {name}\nFilename: {name}\n\n'
                            for name, _, kind in files if kind == KIND_PKG).encode()
            files.append((f'dists/stable/main/binary-{unit.arch}/Packages', index, KIND_META))
            entries = []
            for name, body, kind in files:
                cs = hashlib.sha256(body).hexdigest()
                pool.add(body, cs, 'sha256', name.rsplit('/', 1)[-1], len(body))
                entries.append((cs, name, kind))
            pool.reconcile_refs(unit.name, entries, src.identity)
            pool.record_mirror(unit)
    cfg = config.Config(settings=config.Settings(mirror_dir=md), sources=[src])
    return cfg, md, units


def test_repository_slice_equals_network_walk_for_each_arch(tmp_path):
    from mmw.core import config
    from mmw.core.repository_points import record_repository, slice_files
    from mmw.core.snapshots import walk_tree
    cfg, md, units = apt_repository(tmp_path)
    with Pool(md) as pool:
        recorded = record_repository(pool, units[0].source, units, md, '20261101T120000Z')
        assert len(recorded) == 1
        pid = recorded[0][0]
        for arch in ('amd64', 'arm64'):
            net = config.NetworkConfig(name=arch, select=[config.NetworkSelect(source=units[0].source, arches=[arch])])
            rows, _, _ = walk_tree(net, cfg, md, pool)
            mine = [u for u in units if u.arch == arch]
            assert slice_files(pool, pid, [(u.source, u.dims) for u in mine]) == rows
        assert not record_repository(pool, units[0].source, units, md, '20261102T120000Z')[0][1]


def test_upstream_under_base_is_frozen_and_sliced(tmp_path):
    from mmw.core import config, upstream
    from mmw.core.repository_points import record_repository, slice_files
    from mmw.core.snapshots import walk_tree
    cfg, md, units = apt_repository(tmp_path)
    upstream.store(md, upstream.repository_group(units[0]),
                   'https://apt.test/debian/archive-key.asc', b'upstream key')
    with Pool(md) as pool:
        recorded = record_repository(pool, units[0].source, units, md, '20261101T120000Z')
        for arch in ('amd64', 'arm64'):
            net = config.NetworkConfig(name=arch, select=[config.NetworkSelect(source=units[0].source, arches=[arch])])
            expected, _, _ = walk_tree(net, cfg, md, pool)
            selected = [(u.source, u.dims) for u in units if u.arch == arch]
            actual = {p: c for pid, _, _ in recorded for p, c in slice_files(pool, pid, selected).items()}
            assert actual == expected
            assert actual['apt.test/debian/archive-key.asc']


def test_nested_repository_bases_do_not_claim_each_others_files(tmp_path):
    from mmw.core import config
    from mmw.core.points import by_repo
    from mmw.core.repository_points import record_repository, slice_files
    from mmw.core.snapshots import walk_tree
    cfg, md, units = apt_repository(tmp_path)
    source = config.SourceConfig(url='https://apt.test/debian/nested', provider='apt-flat')
    nested = source.to_mirrors()[0]
    with Pool(md) as pool:
        # Reuse a real blob at an overlapping base; its ownership follows the
        # deepest base, rather than whichever repository is enumerated first.
        checksum = pool.mirror_files([units[0].name])[0][4]
        pool.reconcile_refs(nested.name, [(checksum, 'Packages', 'meta')], source.identity)
        pool.record_mirror(nested)
        cfg = cfg.model_copy(update={'sources': [*cfg.sources, source]})
        net = config.NetworkConfig(name='all')
        rows, settings, _ = walk_tree(net, cfg, md, pool)
        attributed = by_repo(rows, settings, net.name)
        for repo, mine in ((units[0].source, units), (nested.source, [nested])):
            result = record_repository(pool, repo, mine, md, '20261101T120000Z')
            sliced = {p: c for pid, _, _ in result
                      for p, c in slice_files(pool, pid, [(u.source, u.dims) for u in mine]).items()}
            assert sliced == attributed[repo]


def test_sync_without_network_records_repository_history(tmp_path):
    from mmw.core.points import record_points_after_sync
    cfg, md, units = apt_repository(tmp_path)
    said = []
    record_points_after_sync(cfg, md, {units[0].source}, say=said.append)
    with Pool(md) as pool:
        assert len(pool.repository_points(units[0].source)) == 1
    assert any('repository point' in line for line in said)


def test_two_networks_link_one_repository_point(tmp_path):
    from mmw.core import config, points
    cfg, md, units = apt_repository(tmp_path)
    nets = [config.NetworkConfig(name=arch, select=[config.NetworkSelect(source=units[0].source, arches=[arch])])
            for arch in ('amd64', 'arm64')]
    cfg = cfg.model_copy(update={'networks': nets})
    points.record_points_after_sync(cfg, md, {units[0].source}, say=lambda _: None)
    with Pool(md) as pool:
        assert len(pool.repository_points(units[0].source)) == 1
        assert pool.network_points('amd64')[0].id == pool.network_points('arm64')[0].id
        assert pool.network_slice('amd64', 1) != pool.network_slice('arm64', 1)


def test_incomplete_track_records_nothing(tmp_path):
    from mmw.core.points import record_points_after_sync
    from mmw.core.downloader import MirrorSummary
    cfg, md, units = apt_repository(tmp_path)
    outcomes = [MirrorSummary(mirror=units[0].name), MirrorSummary(mirror=units[1].name, failed=1)]
    record_points_after_sync(cfg, md, {units[0].source}, outcomes=outcomes, attempted=units, say=lambda _: None)
    with Pool(md) as pool:
        assert pool.repository_points(units[0].source) == []


def test_unattempted_synced_sibling_is_carried_forward(tmp_path):
    from mmw.core.points import record_points_after_sync
    from mmw.core.downloader import MirrorSummary
    cfg, md, units = apt_repository(tmp_path)
    record_points_after_sync(cfg, md, {units[0].source}, outcomes=[MirrorSummary(mirror=units[0].name)],
                             attempted=units[:1], say=lambda _: None)
    with Pool(md) as pool:
        assert len(pool.repository_points(units[0].source)) == 1


def test_plan_past_points_without_network_fetches_all_units(tmp_path):
    from mmw.core.past_points import plan_past_points
    cfg, md, units = apt_repository(tmp_path)
    plan = plan_past_points(cfg, md, units[0].source)
    assert {u.name for u in plan.units} == {u.name for u in units}
    assert plan.networks == []


def test_replay_uses_recorded_network_slice(tmp_path):
    from mmw.core import config, points
    from mmw.core.snapshots import walk_tree
    cfg, md, units = apt_repository(tmp_path)
    net = config.NetworkConfig(name='lab', nginx=False, certs=False,
                               select=[config.NetworkSelect(source=units[0].source, arches=['amd64'])])
    cfg = cfg.model_copy(update={'networks': [net]})
    points.record_points_after_sync(cfg, md, {units[0].source}, say=lambda _: None)
    with Pool(md) as pool:
        expected, _, _ = walk_tree(net, cfg, md, pool)
        rows, _ = points.moment_rows(pool, net.name, '20990101T000000Z')
        assert {p: cs for p, cs, _, _ in rows} == expected


def test_bundle_lists_only_network_slice_and_widening_sends_new_blobs(tmp_path):
    from mmw.core import config, points
    from mmw.core.publisher.bundle import bundle_network
    from test_bundle_mmw import _read
    cfg, md, units = apt_repository(tmp_path)
    net = config.NetworkConfig(name='lab', nginx=False, certs=False,
                               select=[config.NetworkSelect(source=units[0].source, arches=['amd64'])])
    cfg = cfg.model_copy(update={'networks': [net]})
    points.record_points_after_sync(cfg, md, {units[0].source}, say=lambda _: None)
    first = bundle_network(net, cfg, md, out_path=tmp_path / 'one.tar')
    _, files, _, sent, _ = _read(first.out_path)
    paths = files[units[0].source]
    assert 'apt.test/debian/pool/amd64.deb' in paths
    assert 'apt.test/debian/pool/arm64.deb' not in paths
    wide = net.model_copy(update={'select': []})
    cfg = cfg.model_copy(update={'networks': [wide]})
    second = bundle_network(wide, cfg, md, out_path=tmp_path / 'two.tar')
    _, files, _, carried, _ = _read(second.out_path)
    assert 'apt.test/debian/pool/arm64.deb' in files[units[0].source]
    assert not sent & carried
    with Pool(md) as pool:
        assert len(pool.repository_points(units[0].source)) == 1


def test_widening_preserves_old_recordings_and_snapshot_links(tmp_path, monkeypatch):
    from mmw.core import config, points
    from mmw.core.publisher import manifest
    from mmw.core.snapshots import take_snapshot, walk_tree
    cfg, md, units = apt_repository(tmp_path)
    net = config.NetworkConfig(name='lab', nginx=False, certs=False,
                               select=[config.NetworkSelect(source=units[0].source, arches=['amd64'])])
    cfg = cfg.model_copy(update={'networks': [net]})
    monkeypatch.setattr(manifest, 'now_stamp', lambda: '20261102T120000Z')
    points.record_points_after_sync(cfg, md, {units[0].source}, say=lambda _: None)
    with Pool(md) as pool:
        original, _, _ = walk_tree(net, cfg, md, pool)
        pid = pool.repository_points(units[0].source)[0].id
    snap = take_snapshot(net, cfg, md)
    with Pool(md) as pool:
        assert pool.snapshot_point_ids(snap.id) == [pid]
    wide = net.model_copy(update={'select': []})
    cfg = cfg.model_copy(update={'networks': [wide]})
    points.record_network_points(wide, cfg, md, stamp='20261104T120000Z')
    with Pool(md) as pool:
        then, _ = points.moment_rows(pool, net.name, '20261103T120000Z')
        now, _ = points.moment_rows(pool, net.name, '20261105T120000Z')
        assert {p: c for p, c, _, _ in then} == original
        assert 'apt.test/debian/pool/arm64.deb' in {p for p, *_ in now}


def test_network_added_later_does_not_acquire_repository_past(tmp_path):
    from dataclasses import replace
    from mmw.core import config, points
    from mmw.core.repository_points import record_repository
    cfg, md, units = apt_repository(tmp_path)
    with Pool(md) as pool:
        recorded = record_repository(pool, units[0].source, units, md, '20261001T000000Z')
        older = replace(recorded[0][2], ident='release:older', date='20260901T000000Z')
        old, _ = pool.record_point(units[0].source, older, '20261001T000000Z')
        pool.record_point_units(old, units[0].source, [(u.dims, older.files) for u in units])
    points.record_points_after_sync(cfg, md, {units[0].source}, say=lambda _: None)
    net = config.NetworkConfig(name='new', nginx=False, certs=False)
    cfg = cfg.model_copy(update={'networks': [net]})
    points.record_network_points(net, cfg, md)
    with Pool(md) as pool:
        assert old not in {p.id for p in pool.network_points(net.name)}
        assert len(pool.repository_points(units[0].source)) == 2


def test_slice_oracle_when_successful_units_saw_different_releases(tmp_path):
    """Upstream may republish between two successful architecture syncs."""
    import hashlib
    from mmw.core import config
    from mmw.core.repository_points import record_repository, slice_files
    from mmw.core.snapshots import walk_tree
    cfg, md, units = apt_repository(tmp_path)
    with Pool(md) as pool:
        unit = units[1]
        entries = [(cs, path, kind) for path, _, _, kind, cs in pool.mirror_files([unit.name])
                   if path != 'dists/stable/Release']
        release = b'Suite: stable\nDate: Mon, 02 Nov 2026 00:00:00 UTC\n'
        checksum = hashlib.sha256(release).hexdigest()
        pool.add(release, checksum, 'sha256', 'Release', len(release))
        pool.reconcile_refs(unit.name, [*entries, (checksum, 'dists/stable/Release', 'release')], unit.source)
        recorded = record_repository(pool, unit.source, units, md, '20261102T120000Z')
        for selected in [[u] for u in units] + [units]:
            net = config.NetworkConfig(name="selected", nginx=False, certs=False,
                                       select=[config.NetworkSelect(source=selected[0].source, arches=[u.arch for u in selected])])
            expected, _, _ = walk_tree(net, cfg, md, pool)
            actual = {p: c for pid, _, _ in recorded
                      for p, c in slice_files(pool, pid, [(u.source, u.dims) for u in selected]).items()}
            assert actual == expected


def change_release(pool, unit, body):
    import hashlib
    entries = [(cs, path, kind) for path, _, _, kind, cs in pool.mirror_files([unit.name])
               if path != 'dists/stable/Release']
    cs = hashlib.sha256(body).hexdigest()
    pool.add(body, cs, 'sha256', 'Release', len(body))
    pool.reconcile_refs(unit.name, [*entries, (cs, 'dists/stable/Release', 'release')], unit.source)
    return cs


def test_unselected_arch_updates_do_not_create_network_points(tmp_path):
    from mmw.core import config, points
    from mmw.core.downloader import MirrorSummary
    cfg, md, units = apt_repository(tmp_path)
    net = config.NetworkConfig(name='amd', nginx=False, certs=False,
                               select=[config.NetworkSelect(source=units[0].source, arches=['amd64'])])
    cfg = cfg.model_copy(update={'networks': [net]})
    points.record_points_after_sync(cfg, md, {units[0].source}, say=lambda _: None)
    arm = next(u for u in units if u.arch == 'arm64')
    with Pool(md) as pool:
        change_release(pool, arm, b'Suite: stable\nDate: Mon, 02 Nov 2026 00:00:00 UTC\n')
    points.record_points_after_sync(cfg, md, {arm.source}, outcomes=[MirrorSummary(mirror=arm.name)],
                                    attempted=[arm], say=lambda _: None)
    with Pool(md) as pool:
        assert len(pool.repository_points(arm.source)) == 2
        assert len(pool.network_points(net.name)) == 1


def test_every_unit_variant_is_a_gc_root(tmp_path):
    from mmw.core.repository_points import record_repository
    cfg, md, units = apt_repository(tmp_path)
    with Pool(md) as pool:
        change_release(pool, units[1], b'Suite: stable\nDate: Mon, 02 Nov 2026 00:00:00 UTC\n')
        recorded = record_repository(pool, units[0].source, units, md, '20261102T120000Z')
        for unit in units:
            pool.reconcile_refs(unit.name, [], unit.source)
        assert pool.prune_preview([units[0].source]) == (0, 0)
        for pid, _, _ in recorded:
            pool.stub_point(pid)
        assert pool.prune_preview([units[0].source])[0] > 0


def test_first_arch_sync_omits_never_synced_units_then_carries_it_forward(tmp_path):
    from mmw.core import points
    from mmw.core.downloader import MirrorSummary
    from mmw.core.repository_points import slice_files
    cfg, md, units = apt_repository(tmp_path)
    amd, arm = (next(u for u in units if u.arch == arch) for arch in ('amd64', 'arm64'))
    with Pool(md) as pool:
        arm_refs = [(cs, path, kind) for path, _, _, kind, cs in pool.mirror_files([arm.name])]
        pool.reconcile_refs(arm.name, [], arm.source)
    points.record_points_after_sync(cfg, md, {amd.source}, outcomes=[MirrorSummary(mirror=amd.name)],
                                    attempted=[amd], say=lambda _: None)
    with Pool(md) as pool:
        first = pool.repository_points(amd.source)[0]
        assert slice_files(pool, first.id, [(arm.source, arm.dims)]) == {}
        amd_files = slice_files(pool, first.id, [(amd.source, amd.dims)])
        assert 'apt.test/debian/pool/amd64.deb' in amd_files
        pool.reconcile_refs(arm.name, arm_refs, arm.source)
        change_release(pool, arm, b'Suite: stable\nDate: Mon, 02 Nov 2026 00:00:00 UTC\n')
    points.record_points_after_sync(cfg, md, {arm.source}, outcomes=[MirrorSummary(mirror=arm.name)],
                                    attempted=[arm], say=lambda _: None)
    with Pool(md) as pool:
        current = pool.repository_points(arm.source)[0]
        assert len(pool.repository_points(arm.source)) == 2
        assert slice_files(pool, current.id, [(amd.source, amd.dims)]) == amd_files
        assert 'apt.test/debian/pool/arm64.deb' in slice_files(pool, current.id, [(arm.source, arm.dims)])


def test_differing_releases_apply_equals_publish_for_all_selections(tmp_path):
    from mmw.core import config, points
    from mmw.core.publisher import publish_network
    from mmw.core.publisher.bundle import bundle_network
    from mmw.site import home as H
    from mmw.site.apply import apply_bundle
    from test_site_apply import _tree
    cfg, md, units = apt_repository(tmp_path)
    with Pool(md) as pool:
        change_release(pool, units[1], b'Suite: stable\nDate: Mon, 02 Nov 2026 00:00:00 UTC\n')
    points.record_points_after_sync(cfg, md, {units[0].source}, say=lambda _: None)
    for arches in (['amd64'], ['arm64'], ['amd64', 'arm64']):
        name = '-'.join(arches)
        net = config.NetworkConfig(name=name, nginx=False, certs=False,
                                   select=[config.NetworkSelect(source=units[0].source, arches=arches)])
        network_cfg = cfg.model_copy(update={'networks': [net]})
        bundle = bundle_network(net, network_cfg, md, out_path=tmp_path / f'{name}.tar')
        root = tmp_path / name / 'site'
        home = H.init_root(root, name='main', set_id='s1', network=net.name)
        apply_bundle(home, str(bundle.out_path), say=lambda *_: None)
        published = tmp_path / name / 'published'
        result = publish_network(net.model_copy(update={'root': published}), network_cfg, md)
        assert not result.errors
        assert _tree(root) == _tree(published)


def test_incomplete_sync_transfers_the_last_complete_repository_point(tmp_path):
    import json
    import tarfile
    from mmw.core import config, points
    from mmw.core.downloader import MirrorSummary
    from mmw.core.publisher.bundle import bundle_network
    cfg, md, units = apt_repository(tmp_path)
    net = config.NetworkConfig(name='lab', nginx=False, certs=False)
    cfg = cfg.model_copy(update={'networks': [net]})
    points.record_points_after_sync(cfg, md, {units[0].source}, say=lambda _: None)
    with Pool(md) as pool:
        change_release(pool, units[1], b'Suite: stable\nDate: Mon, 02 Nov 2026 00:00:00 UTC\n')
    points.record_points_after_sync(cfg, md, {units[0].source},
                                    outcomes=[MirrorSummary(mirror=u.name, failed=1) for u in units],
                                    attempted=units, say=lambda _: None)
    with Pool(md) as pool:
        assert len(pool.repository_points(units[0].source)) == 1
    result = bundle_network(net, cfg, md, out_path=tmp_path / 'complete.tar')
    with tarfile.open(result.out_path) as tar:
        header = json.loads(tar.extractfile('.mmw/manifest.jsonl').readline())
    assert {p['date'][:8] for p in header['points'][units[0].source]['points']} == {'20261101'}
    with Pool(md) as pool:
        assert len(pool.repository_points(units[0].source)) == 1


def test_past_plan_includes_configured_units_not_synced_yet(tmp_path):
    from mmw.core.past_points import plan_past_points
    cfg, md, units = apt_repository(tmp_path)
    cfg.sources[0].arches.append('i386')
    plan = plan_past_points(cfg, md, units[0].source)
    assert {u.arch for u in plan.units} == {'amd64', 'arm64', 'i386'}


def test_network_recording_does_not_rewalk_recorded_repository_groups(tmp_path, monkeypatch):
    from mmw.core import config, points
    from mmw.core.publisher import links
    cfg, md, units = apt_repository(tmp_path)
    points.record_points_after_sync(cfg, md, {units[0].source}, say=lambda _: None)
    net = config.NetworkConfig(name='new', nginx=False, certs=False)
    cfg = cfg.model_copy(update={'networks': [net]})

    def unexpected(*args, **kwargs):
        raise AssertionError('recorded repository must be sliced, not walked again')
    monkeypatch.setattr(links, 'publish_group', unexpected)
    report = points.record_network_points(net, cfg, md)
    assert report.new


def test_empty_dims_are_a_real_unit_not_a_shared_file(tmp_path):
    from mmw.core.repository_points import slice_files
    with Pool(tmp_path) as pool:
        pid, _ = pool.record_point('R', draft(), 'now')
        pool.record_unit_files(pid, 'R', [({}, {'h/pool/a.deb': ('a', 'pkg', False)}),
                                         (None, {'h/shared': ('shared', 'meta', False)})])
        assert slice_files(pool, pid, [('R', {})]) == {'h/pool/a.deb': 'a', 'h/shared': 'shared'}
        assert slice_files(pool, pid, [('R', {'arch': 'amd64'})]) == {'h/shared': 'shared'}


def test_since_uses_slice_release_date_not_another_archs_date(tmp_path):
    from mmw.core import config, points
    from mmw.core.publisher.bundle import resolve_since
    from mmw.core.repository_points import files_for_settings
    cfg, md, units = apt_repository(tmp_path)
    with Pool(md) as pool:
        change_release(pool, units[1], b'Suite: stable\nDate: Mon, 02 Nov 2026 00:00:00 UTC\n')
    points.record_points_after_sync(cfg, md, {units[0].source}, say=lambda _: None)
    net = config.NetworkConfig(name='amd', nginx=False, certs=False,
                               select=[config.NetworkSelect(source=units[0].source, arches=['amd64'])])
    cfg = cfg.model_copy(update={'networks': [net]})
    points.record_network_points(net, cfg, md, stamp='20261103T000000Z')
    _, baseline, _, _ = resolve_since(net, md, '2026-11-01')
    with Pool(md) as pool:
        pid = pool.network_points(net.name)[0].id
        expected = set(files_for_settings(pool, pid, pool.settings_at(net.name, '99999999T999999Z')).values())
        assert baseline == expected
        assert pool.network_slice_date(net.name, pid) == '20261101T000000Z'
        pool.stub_point(pid)
    import pytest
    from mmw.core.publisher.bundle import NothingToStandOn
    with pytest.raises(NothingToStandOn):
        resolve_since(net, md, '2026-11-01')


def test_later_success_does_not_carry_forward_another_units_failed_refs(tmp_path):
    from mmw.core import points
    from mmw.core.downloader import MirrorSummary
    from mmw.core.repository_points import current_repository_points, slice_files
    cfg, md, units = apt_repository(tmp_path)
    amd, arm = (next(u for u in units if u.arch == arch) for arch in ('amd64', 'arm64'))
    points.record_points_after_sync(cfg, md, {amd.source}, say=lambda _: None)
    with Pool(md) as pool:
        before = current_repository_points(pool, amd.source)[0]
        old_arm = slice_files(pool, before, [(arm.source, arm.dims)])
        change_release(pool, arm, b'Suite: stable\nDate: Mon, 02 Nov 2026 00:00:00 UTC\n')
    points.record_points_after_sync(cfg, md, {arm.source}, outcomes=[MirrorSummary(mirror=arm.name, failed=1)],
                                    attempted=[arm], say=lambda _: None)
    with Pool(md) as pool:
        change_release(pool, amd, b'Suite: stable\nDate: Tue, 03 Nov 2026 00:00:00 UTC\n')
    points.record_points_after_sync(cfg, md, {amd.source}, outcomes=[MirrorSummary(mirror=amd.name)],
                                    attempted=[amd], say=lambda _: None)
    with Pool(md) as pool:
        latest = current_repository_points(pool, amd.source)[0]
        assert slice_files(pool, latest, [(arm.source, arm.dims)]) == old_arm


def test_first_failed_unit_is_not_adopted_by_a_later_independent_sync(tmp_path):
    from mmw.core import points
    from mmw.core.downloader import MirrorSummary
    from mmw.core.repository_points import current_repository_points, slice_files
    cfg, md, units = apt_repository(tmp_path)
    amd, arm = (next(u for u in units if u.arch == arch) for arch in ('amd64', 'arm64'))
    points.record_points_after_sync(cfg, md, {arm.source}, outcomes=[MirrorSummary(mirror=arm.name, failed=1)],
                                    attempted=[arm], say=lambda _: None)
    points.record_points_after_sync(cfg, md, {amd.source}, outcomes=[MirrorSummary(mirror=amd.name)],
                                    attempted=[amd], say=lambda _: None)
    with Pool(md) as pool:
        latest = current_repository_points(pool, amd.source)[0]
        assert slice_files(pool, latest, [(arm.source, arm.dims)]) == {}


def test_repository_history_failure_does_not_fail_a_completed_sync(tmp_path, monkeypatch):
    from mmw.core import points
    cfg, md, units = apt_repository(tmp_path)
    said = []

    def unavailable(*args, **kwargs):
        raise OSError('history database unavailable')
    monkeypatch.setattr(points, 'Pool', unavailable)
    assert points.record_points_after_sync(cfg, md, {units[0].source}, say=said.append) == []
    assert any('history database unavailable' in line for line in said)


def test_rederived_unit_names_keep_one_coordinate_and_match_the_walk(tmp_path):
    from mmw.core import config
    from mmw.core.repository_points import record_repository, slice_files
    from mmw.core.snapshots import walk_tree
    cfg, md, units = apt_repository(tmp_path)
    amd = next(u for u in units if u.arch == 'amd64')
    alias = amd.model_copy(update={'name': 'renamed-' + amd.name})
    with Pool(md) as pool:
        entries = [(cs, path, kind) for path, _, _, kind, cs in pool.mirror_files([amd.name])]
        pool.reconcile_refs(alias.name, entries, alias.source)
        pool.record_mirror(alias)
        change_release(pool, alias, b'Suite: stable\nDate: Mon, 02 Nov 2026 00:00:00 UTC\n')
        recorded = record_repository(pool, amd.source, [amd, alias], md, '20261102T120000Z')
        net = config.NetworkConfig(name='amd', nginx=False, certs=False,
                                   select=[config.NetworkSelect(source=amd.source, arches=['amd64'])])
        expected, _, _ = walk_tree(net, cfg, md, pool)
        actual = {p: c for pid, _, _ in recorded for p, c in slice_files(pool, pid, [(amd.source, amd.dims)]).items()}
        assert actual == expected


def test_bundle_preserves_existing_point_header_fields(tmp_path):
    from mmw.core import config, points
    from mmw.core.publisher.bundle import bundle_network
    from test_bundle_mmw import _read
    cfg, md, units = apt_repository(tmp_path)
    with Pool(md) as pool:
        for unit in units:
            change_release(pool, unit, b'Origin: Debian\nLabel: Debian\nSuite: stable\nDate: Sun, 01 Nov 2026 00:00:00 UTC\n')
    net = config.NetworkConfig(name='lab', nginx=False, certs=False)
    cfg = cfg.model_copy(update={'networks': [net]})
    points.record_points_after_sync(cfg, md, {units[0].source}, say=lambda _: None)
    result = bundle_network(net, cfg, md, out_path=tmp_path / 'fields.tar')
    header, _, _, _, _ = _read(result.out_path)
    entry = header['points'][units[0].source]['points'][0]
    assert entry['fields'] == {'Suite': 'stable'}
    assert entry['ident'].startswith('release:')
    assert entry['date'] == '20261101T000000Z'
