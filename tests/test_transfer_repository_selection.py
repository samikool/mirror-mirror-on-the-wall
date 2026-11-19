"""A repository sync is usable before a network has its own recording."""
import json
import tarfile

import pytest
from typer.testing import CliRunner

from mmw.cli import app
from mmw.core import config, points
from mmw.core.pool import Pool
from mmw.core.publisher.bundle import bundle_network
from mmw.core.transfer_ranges import preview_transfer
from test_repository_points import apt_repository


@pytest.fixture
def synced(tmp_path):
    cfg, md, units = apt_repository(tmp_path)
    points.record_points_after_sync(cfg, md, {units[0].source}, say=lambda _: None)
    net = config.NetworkConfig(name='work', nginx=False, certs=False, bundle_dir=tmp_path / 'usb',
                               select=[config.NetworkSelect(source=units[0].source, arches=['amd64'])])
    cfg = cfg.model_copy(update={'networks': [net]})
    config.save(cfg, tmp_path)
    return cfg, net, md, units


def _state(md, net):
    with Pool(md, read_only=True) as pool:
        return (pool.repository_points(), pool.network_points(net.name),
                pool.latest_recordings(net.name), pool.settings_at(net.name, '99999999T999999Z'),
                pool.bundles(net.name))


@pytest.mark.parametrize('until', [None, '2026-11-02'])
def test_preview_reads_repository_points_without_recording_network(synced, until):
    cfg, net, md, units = synced
    before = _state(md, net)
    assert before[0] and before[1:] == ([], {}, None, [])
    plan = preview_transfer(net, md, cfg=cfg, until=until, full=True)
    assert len(plan.rows) == 1 and plan.rows[0].repo == units[0].source
    assert plan.rows[0].to_date.startswith('20261101') and plan.bytes > 0
    assert _state(md, net) == before


@pytest.mark.parametrize('verb', ['bundle', 'stage'])
def test_cli_preview_after_sync_before_network_created(synced, tmp_path, verb):
    _, net, md, _ = synced
    before = _state(md, net)
    result = CliRunner().invoke(app, ['--config', str(tmp_path), verb, '-n', net.name, '--dry-run'])
    assert result.exit_code == 0, result.output
    assert '2026-11-01' in result.output and 'new blobs' in result.output
    assert _state(md, net) == before


@pytest.mark.parametrize('until', [None, '2026-11-02'])
def test_transfer_uses_the_same_repository_slice(synced, tmp_path, until):
    cfg, net, md, units = synced
    before = _state(md, net)[0]
    estimate = preview_transfer(net, md, cfg=cfg, until=until, full=True)
    result = bundle_network(net, cfg, md, out_path=tmp_path / 'bundle.tar', until=until, full=True)
    with tarfile.open(result.out_path) as tar:
        rows = [json.loads(ln) for ln in tar.extractfile('.mmw/manifest.jsonl').read().decode().splitlines()]
        payload = [m for m in tar if m.name.startswith('blobs/') and m.isfile()]
    assert units[0].source in rows[0]['points']
    assert any(row.get('p', '').endswith('/pool/amd64.deb') for row in rows[1:])
    assert not any(row.get('p', '').endswith('/pool/arm64.deb') for row in rows[1:])
    assert sum(m.size for m in payload) == estimate.bytes
    assert _state(md, net)[0] == before
    assert _state(md, net)[1:4] == ([], {}, None)


def test_tui_preview_finds_the_synced_repository(synced):
    from textual.widgets import Select
    from test_snapshot_tui import _run
    from waiting import shows, until
    cfg, net, md, _ = synced
    before = _state(md, net)

    async def check(app, pilot):
        await pilot.press('t')
        await until(pilot, lambda: type(app.screen).__name__ == 'BundlePickPage' and app.screen.query('#pick-to'))
        app.screen.query_one('#pick-installer', Select).value = 'no'
        await shows(pilot, lambda: app.screen.query_one('#pick-estimate').render(), '2026-11-01', 'blobs')

    _run(cfg, md.parent, check)
    assert _state(md, net) == before


@pytest.mark.parametrize('until', [None, '2026-11-02'])
def test_imported_repository_points_without_live_refs_can_transfer(synced, tmp_path, until):
    cfg, net, md, units = synced
    with Pool(md) as pool:
        # A historical import retains points without setting live refs or
        # repository_current, just like Add a past point.
        pool._conn.execute('DELETE FROM repository_current')
        pool._conn.commit()
        for unit in units:
            pool.reconcile_refs(unit.name, [], unit.source)
        pool._conn.commit()
    plan = preview_transfer(net, md, cfg=cfg, until=until, full=True)
    assert plan.rows and plan.bytes > 0
    result = bundle_network(net, cfg, md, out_path=tmp_path / 'past.tar', until=until, full=True)
    assert result.out_path.is_file()


def _point(cfg, md, unit, day, with_package):
    from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE
    from test_repository_points import change_release
    import hashlib
    with Pool(md) as pool:
        entries = [(cs, path, kind) for path, _, _, kind, cs in pool.mirror_files([unit.name])
                   if not path.endswith('/Packages') and path != 'pool/amd64.deb']
        names = ['shared'] + (['amd64'] if with_package else [])
        index = ''.join(f'Package: {name}\nFilename: pool/{name}.deb\n\n' for name in names).encode()
        cs = hashlib.sha256(index).hexdigest()
        pool.add(index, cs, 'sha256', 'Packages', len(index))
        entries.append((cs, 'dists/stable/main/binary-amd64/Packages', KIND_META))
        if with_package:
            body = b'amd64'
            cs = hashlib.sha256(body).hexdigest()
            pool.add(body, cs, 'sha256', 'amd64.deb', len(body))
            entries.append((cs, 'pool/amd64.deb', KIND_PKG))
        assert any(kind == KIND_RELEASE for _, _, kind in entries)
        pool.reconcile_refs(unit.name, entries, unit.source)
        change_release(pool, unit, f'Suite: stable\nDate: Mon, {day:02} Nov 2026 00:00:00 UTC\n'.encode())
    points.record_points_after_sync(cfg.model_copy(update={'networks': []}), md, {unit.source}, say=lambda _: None)


@pytest.mark.parametrize('history', [False, True])
@pytest.mark.parametrize('stage', [False, True])
@pytest.mark.parametrize('since', [None, '2026-11-01'])
def test_x_to_z_applies_from_y_when_x_file_was_removed_then_reintroduced(synced, tmp_path, history, stage, since):
    import hashlib
    from mmw.site import home as H
    from mmw.site.apply import apply_bundle
    from mmw.site.db import SiteDB
    from test_site_apply_timeline import _served_day
    cfg, net, md, units = synced
    unit = next(u for u in units if u.arch == 'amd64')
    bundle_network(net, cfg, md, out_path=tmp_path / 'x-not-delivered.tar', full=True)
    _point(cfg, md, unit, 2, False)
    y = bundle_network(net, cfg, md, out_path=tmp_path / 'y.tar', full=True).out_path
    home = H.init_root(tmp_path / 'far', name='main', set_id='work', network=net.name)
    apply_bundle(home, str(y), say=lambda *_: None)
    assert not (tmp_path / 'far/apt.test/debian/pool/amd64.deb').exists()
    _point(cfg, md, unit, 3, True)
    before = _state(md, net)
    plan = preview_transfer(net, md, cfg=cfg, since=since, until='2026-11-03', history=history)
    assert len(plan.rows[0].available) == 3
    result = bundle_network(net, cfg, md, out_path=tmp_path / ('range' if stage else 'range.tar'),
                                since=since, until='2026-11-03', history=history, stage=stage)
    assert _state(md, net)[:4] == before[:4]
    checksum = hashlib.sha256(b'amd64').hexdigest()
    if stage:
        assert (result.out_path / f'blobs/{checksum[:2]}/{checksum}').is_file()
    else:
        with tarfile.open(result.out_path) as tar:
            assert f'blobs/{checksum[:2]}/{checksum}' in tar.getnames()
    if history and not stage:
        from test_site_tui import _run, _review_and_go
        from mmw.site import recents
        from site_run import run_done
        from waiting import until

        async def apply_in_tui(app, pilot):
            await _review_and_go(app, pilot, result.out_path)
            await until(pilot, lambda: app.screen.finished)
            assert '2026-11-03' in run_done(app)
            assert 'incomplete' not in run_done(app)

        recents.remember(str(tmp_path / 'far'))
        _run(home, apply_in_tui)
    else:
        applied = apply_bundle(home, str(result.out_path), say=lambda *_: None)
        assert not applied.refused and not applied.incomplete
    assert (tmp_path / 'far/apt.test/debian/pool/amd64.deb').read_bytes() == b'amd64'
    with SiteDB(home) as db:
        assert _served_day(db, unit.source) == '20261103'


def test_tui_selects_an_arbitrary_repository_range(synced):
    from textual.widgets import Input, Select
    from pickers import confirm_picker
    from test_snapshot_tui import _run, _finished
    from waiting import shows, until
    cfg, net, md, units = synced
    unit = next(u for u in units if u.arch == 'amd64')
    _point(cfg, md, unit, 2, False)
    _point(cfg, md, unit, 3, True)
    before = _state(md, net)

    async def check(app, pilot):
        await pilot.press('t')
        await until(pilot, lambda: type(app.screen).__name__ == 'BundlePickPage' and app.screen.query('#pick-to'))
        app.screen.query_one('#pick-from', Select).value = 'date'
        app.screen.query_one('#pick-since', Input).value = '2026-11-01'
        app.screen.query_one('#pick-to', Select).value = 'date'
        app.screen.query_one('#pick-until', Input).value = '2026-11-03'
        app.screen.query_one('#pick-installer', Select).value = 'no'
        await shows(pilot, lambda: app.screen.query_one('#pick-estimate').render(), '2026-11-01', '2026-11-03')
        await confirm_picker(app, pilot, 'BundlePickPage')
        await until(pilot, lambda: _finished(app))
        assert app.active_transfer.outcome == 'ok', app.notes

    _run(cfg, md.parent, check)
    assert _state(md, net)[:4] == before[:4]


def test_selected_repository_does_not_require_syncing_other_repositories(synced, tmp_path):
    cfg, net, md, units = synced
    other = config.SourceConfig(url='https://unsynced.test/ubuntu', suites=['stable'],
                                components=['main'], arches=['amd64'])
    net = net.model_copy(update={'select': [*net.select, config.NetworkSelect(source=other.identity)]})
    cfg = cfg.model_copy(update={'sources': [*cfg.sources, other], 'networks': [net]})
    config.save(cfg, tmp_path)
    result = CliRunner().invoke(app, ['--config', str(tmp_path), 'bundle', '-n', net.name,
                                      '--repo', units[0].source, '--out', str(tmp_path / 'one.tar')])
    assert result.exit_code == 0, result.output
    with tarfile.open(tmp_path / 'one.tar') as tar:
        header = json.loads(tar.extractfile('.mmw/manifest.jsonl').readline())
    assert set(header['points']) == {units[0].source}


def test_backwards_range_is_rejected_before_writing(synced, tmp_path):
    cfg, net, md, _ = synced
    before = _state(md, net)
    with pytest.raises(ValueError, match='From must be at or before To'):
        preview_transfer(net, md, cfg=cfg, since='2026-11-03', until='2026-11-01')
    with pytest.raises(ValueError, match='From must be at or before To'):
        bundle_network(net, cfg, md, since='2026-11-03', until='2026-11-01', out_path=tmp_path / 'bad.tar')
    assert not (tmp_path / 'bad.tar').exists()
    assert _state(md, net) == before


def test_an_unchanged_suite_can_predate_both_requested_dates(synced, tmp_path):
    cfg, net, md, _ = synced
    plan = preview_transfer(net, md, cfg=cfg, since='2026-11-02', until='2026-11-03')
    assert plan.rows[0].to_date.startswith('20261101')
    assert plan.rows[0].from_date == plan.rows[0].to_date
    result = bundle_network(net, cfg, md, since='2026-11-02', until='2026-11-03',
                            out_path=tmp_path / 'unchanged.tar')
    assert result.out_path.is_file()


def test_a_missing_last_selection_ledger_carries_more_instead_of_refusing(synced, tmp_path):
    from mmw.core.publisher.manifest import ledger_dir, MANIFEST_SUFFIX
    cfg, net, md, units = synced
    bundle_network(net, cfg, md, out_path=tmp_path / 'narrow.tar')
    with Pool(md, read_only=True) as pool:
        assert pool.settings_at(net.name, '99999999T999999Z') is None
        last = pool.bundles(net.name)[0]
    # A convenience record is not a prerequisite for an independent range.
    (ledger_dir(md, net.name) / f'{last.to_stamp}{MANIFEST_SUFFIX}').write_text('[]\n')
    wide = net.model_copy(update={'select': []})
    cfg = cfg.model_copy(update={'networks': [wide]})
    result = bundle_network(wide, cfg, md, out_path=tmp_path / 'wide.tar')
    with tarfile.open(result.out_path) as tar:
        rows = [json.loads(line) for line in tar.extractfile('.mmw/manifest.jsonl').read().decode().splitlines()]
        assert units[0].source in rows[0]['points']
        assert any(row.get('p', '').endswith('pool/arm64.deb') for row in rows[1:])


def test_live_publish_uses_points_prunes_and_places_packages_before_release(synced, tmp_path):
    from mmw.core.publisher import publish_network
    cfg, net, md, units = synced
    net = net.model_copy(update={'root': tmp_path / 'published'})
    cfg = cfg.model_copy(update={'networks': [net]})
    unit = next(u for u in units if u.arch == 'amd64')
    assert not publish_network(net, cfg, md).errors
    _point(cfg, md, unit, 2, False)
    assert not publish_network(net, cfg, md).errors
    package = net.root / 'apt.test/debian/pool/amd64.deb'
    assert not package.exists()
    _point(cfg, md, unit, 3, True)
    checked = []

    def progress(path, status):
        if path.endswith('/Release') and status == 'linked-meta':
            assert package.read_bytes() == b'amd64'
            checked.append(path)

    result = publish_network(net, cfg, md, progress=progress)
    assert not result.errors and result.mirrors and checked


@pytest.mark.parametrize('door', ['cli', 'tui'])
def test_publish_an_older_repository_point_without_network_recordings(synced, tmp_path, door):
    cfg, net, md, units = synced
    net = net.model_copy(update={'root': tmp_path / 'published'})
    cfg = cfg.model_copy(update={'networks': [net]})
    unit = next(u for u in units if u.arch == 'amd64')
    _point(cfg, md, unit, 2, False)
    before = _state(md, net)
    config.save(cfg, tmp_path)
    if door == 'cli':
        result = CliRunner().invoke(app, ['--config', str(tmp_path), 'publish', '--quiet', '-n', net.name,
                                          '--as-of', '2026-11-01'])
        assert result.exit_code == 0, result.output
    else:
        from textual.widgets import Input, Select
        from pickers import confirm_picker
        from test_snapshot_tui import _run, _finished
        from waiting import until

        async def check(app, pilot):
            await pilot.press('p')
            await until(pilot, lambda: type(app.screen).__name__ == 'PublishPickPage' and app.screen.query('#pick-target'))
            app.screen.query_one('#pick-target', Select).value = 'date'
            app.screen.query_one('#pick-when', Input).value = '2026-11-01'
            await confirm_picker(app, pilot, 'PublishPickPage')
            await until(pilot, lambda: _finished(app))
            assert app.active_transfer.outcome == 'ok', app.notes

        _run(cfg, tmp_path, check)
    assert (net.root / 'apt.test/debian/pool/amd64.deb').read_bytes() == b'amd64'
    assert not (net.root / 'apt.test/debian/pool/arm64.deb').exists()
    assert _state(md, net) == before


def test_new_network_carries_keys_cached_image_and_tls_configuration(synced, tmp_path):
    from mmw.core.publisher import publish_as_of
    from mmw.site import home as H
    from mmw.site.apply import apply_bundle
    cfg, net, md, _ = synced
    net = net.model_copy(update={'nginx': True, 'certs': True, 'root': tmp_path / 'published'})
    cfg = cfg.model_copy(update={'networks': [net]})
    keys = md / 'keys/apt'
    keys.mkdir(parents=True)
    (keys / 'archive.asc').write_bytes(b'repository public key')
    cache = md / 'nginx-image'
    cache.mkdir()
    (cache / 'cached.tar').write_bytes(b'cached nginx image')
    plan = preview_transfer(net, md, cfg=cfg, until='2026-11-02', full=True)
    assert not (cache / 'cached.tar.sha256').exists()     # preview makes no cache writes
    result = bundle_network(net, cfg, md, until='2026-11-02', full=True, out_path=tmp_path / 'tls.tar')
    with tarfile.open(result.out_path) as tar:
        assert not any(m.name.endswith('.key') for m in tar)
        assert sum(m.size for m in tar if m.name.startswith('blobs/') and m.isfile()) == plan.bytes
    published = publish_as_of(net, cfg, md, '2026-11-01')
    assert not published.errors
    root = tmp_path / 'far'
    home = H.init_root(root, name='main', set_id='work', network=net.name)
    applied = apply_bundle(home, str(result.out_path), say=lambda *_: None)
    assert not applied.refused and not applied.incomplete
    for rel in ('keys/apt/archive.asc', 'nginx/nginx-latest-linux-amd64.tar',
                'apt.test/debian/pool/amd64.deb', 'apt.test/debian/dists/stable/Release'):
        assert (root / rel).read_bytes() == (net.root / rel).read_bytes()
    replayed = apply_bundle(home, str(result.out_path), say=lambda *_: None)
    assert not replayed.refused and not replayed.incomplete


def test_apply_warns_about_cleanup_but_serves_z(synced, tmp_path, monkeypatch):
    from mmw.core import disk_io
    from mmw.site import home as H
    from mmw.site.apply import apply_bundle
    from mmw.site.db import SiteDB
    from test_site_apply_timeline import _served_day
    cfg, net, md, units = synced
    root = tmp_path / 'far'
    home = H.init_root(root, name='main', set_id='work', network=net.name)
    x = bundle_network(net, cfg, md, out_path=tmp_path / 'x.tar', full=True)
    apply_bundle(home, str(x.out_path), say=lambda *_: None)
    unit = next(u for u in units if u.arch == 'amd64')
    _point(cfg, md, unit, 2, False)
    z = bundle_network(net, cfg, md, out_path=tmp_path / 'z.tar', since='2026-11-01', until='2026-11-02')
    obsolete = root / 'apt.test/debian/pool/amd64.deb'
    remove = disk_io.ops.remove

    def denied(path):
        if path == obsolete:
            raise PermissionError('cleanup denied')
        return remove(path)

    monkeypatch.setattr(disk_io.ops, 'remove', denied)
    result = apply_bundle(home, str(z.out_path), say=lambda *_: None)
    assert not result.refused and not result.incomplete
    assert unit.source in result.updated
    assert any('could not be removed' in line for line in result.lines)
    with SiteDB(home) as db:
        assert _served_day(db, unit.source) == '20261102'
