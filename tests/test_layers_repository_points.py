"""A package layer leaves the other repository's transfer history intact."""
import hashlib
import json
import tarfile

import pytest
from debs import make_deb

from mmw.core import config, points
from mmw.core.layers import layer_dir
from mmw.core.pool import KIND_PKG, Pool
from mmw.core.publisher import publish_network
from mmw.core.publisher.bundle import bundle_network
from mmw.core.transfer_ranges import preview_transfer
from test_repository_points import apt_repository, change_release

PLAIN_PACKAGE = b'plain repository package'
LAYER_INDEX = 'apt.test/debian/dists/stable/main/binary-amd64/Packages'


@pytest.fixture
def mixed_network(tmp_path, monkeypatch, request):
    cfg, md, layered_units = apt_repository(tmp_path)
    layered = cfg.sources[0]
    plain = config.SourceConfig(url='https://plain.test/debian', suites=['stable'],
                                components=['main'], arches=['amd64', 'arm64'])
    plain_units = plain.to_mirrors()
    with Pool(md) as pool:
        for original, unit in zip(layered_units, plain_units):
            entries = [(checksum, path, kind) for path, _, _, kind, checksum
                       in pool.mirror_files([original.name])]
            if unit.arch == 'amd64':
                checksum = hashlib.sha256(PLAIN_PACKAGE).hexdigest()
                pool.add(PLAIN_PACKAGE, checksum, 'sha256', 'amd64.deb', len(PLAIN_PACKAGE))
                entries = [(cs, path, kind) for cs, path, kind in entries if path != 'pool/amd64.deb']
                entries.append((checksum, 'pool/amd64.deb', KIND_PKG))
            pool.reconcile_refs(unit.name, entries, unit.source)
            pool.record_mirror(unit)

    net = config.NetworkConfig(
        name='mixed', nginx=False, certs=False,
        layers=[config.NetworkLayer(source=layered.url, signing='unsigned')],
    )
    cfg = cfg.model_copy(update={'sources': [layered, plain], 'networks': [net]})
    make_deb(layer_dir(md, net.name, layered.name) / 'stable/main', 'local-tools', '1.0')
    sources = {layered.identity, plain.identity}
    first_cfg = cfg.model_copy(update={'networks': []}) if getattr(request, 'param', None) == 'late-layer' else cfg
    with monkeypatch.context() as patches:
        patches.setattr('mmw.core.publisher.manifest.now_stamp', lambda: '20261101T120000Z')
        points.record_points_after_sync(first_cfg, md, sources)

    with Pool(md) as pool:
        for unit in [*layered_units, *plain_units]:
            change_release(pool, unit, b'Suite: stable\nDate: Mon, 02 Nov 2026 00:00:00 UTC\n')
    with monkeypatch.context() as patches:
        patches.setattr('mmw.core.publisher.manifest.now_stamp', lambda: '20261102T120000Z')
        second, = points.record_points_after_sync(cfg, md, sources)
    assert second.new

    return cfg, net, md, plain


def test_unlayered_repository_keeps_repository_ranges(mixed_network):
    cfg, net, md, plain = mixed_network
    preview = preview_transfer(net, md, cfg=cfg, since='2026-11-01')
    row = next(row for row in preview.rows if row.repo == plain.identity)
    assert row.from_date == '20261101T000000Z'
    assert row.to_date == '20261102T000000Z'
    assert preview.source == 'repository points as of 2026-11-01'


@pytest.mark.parametrize('mixed_network', ['late-layer'], indirect=True)
def test_unlayered_range_can_start_before_the_first_layer_recording(mixed_network):
    cfg, net, md, plain = mixed_network
    preview = preview_transfer(net, md, cfg=cfg, since='2026-11-01')
    row = next(row for row in preview.rows if row.repo == plain.identity)
    assert row.from_date == '20261101T000000Z'


def _bundled_layer_index(path):
    with tarfile.open(path) as tar:
        manifest = tar.extractfile('.mmw/manifest.jsonl')
        for row in map(json.loads, manifest):
            if row.get('p') == LAYER_INDEX:
                checksum = row['c']
                return tar.extractfile(f'blobs/{checksum[:2]}/{checksum}').read()
    pytest.fail('the layered repository has no Packages entry in the manifest')


def test_layered_repository_still_carries_its_layer(mixed_network, tmp_path):
    cfg, net, md, _ = mixed_network
    bundle = bundle_network(net, cfg, md, out_path=tmp_path / 'mixed.tar', full=True)
    assert b'Package: local-tools\n' in _bundled_layer_index(bundle.out_path)


def test_current_bundle_matches_published_layer_after_local_changes(mixed_network, tmp_path):
    cfg, net, md, _ = mixed_network
    net = net.model_copy(update={'root': tmp_path / 'published'})
    cfg = cfg.model_copy(update={'networks': [net]})
    folder = layer_dir(md, net.name, cfg.sources[0].name) / 'stable/main'
    make_deb(folder, 'new-tool', '1.0')

    current = bundle_network(net, cfg, md, out_path=tmp_path / 'current.tar', full=True)
    assert not publish_network(net, cfg, md).errors
    published_index = (net.root / LAYER_INDEX).read_bytes()
    assert b'Package: new-tool\n' in published_index
    assert _bundled_layer_index(current.out_path) == published_index

    dated = bundle_network(net, cfg, md, out_path=tmp_path / 'dated.tar', full=True, until='2026-11-01')
    dated_index = _bundled_layer_index(dated.out_path)
    assert b'Package: local-tools\n' in dated_index
    assert b'Package: new-tool\n' not in dated_index


def test_current_incremental_bundle_carries_a_new_layer_package(mixed_network, tmp_path):
    cfg, net, md, _ = mixed_network
    folder = layer_dir(md, net.name, cfg.sources[0].name) / 'stable/main'
    package = make_deb(folder, 'new-tool', '1.0')
    checksum = hashlib.sha256(package.read_bytes()).hexdigest()

    current = bundle_network(net, cfg, md, out_path=tmp_path / 'current.tar', since='2026-11-02')
    with tarfile.open(current.out_path) as tar:
        assert f'blobs/{checksum[:2]}/{checksum}' in tar.getnames()
    assert b'Package: new-tool\n' in _bundled_layer_index(current.out_path)


def test_mixed_range_sends_a_package_reintroduced_after_the_start(mixed_network, tmp_path, monkeypatch):
    cfg, net, md, plain = mixed_network
    units = plain.to_mirrors()
    unit = next(unit for unit in units if unit.arch == 'amd64')
    checksum = hashlib.sha256(PLAIN_PACKAGE).hexdigest()
    with Pool(md) as pool:
        entries = [(cs, path, kind) for path, _, _, kind, cs in pool.mirror_files([unit.name])
                   if path != 'pool/amd64.deb']
        pool.reconcile_refs(unit.name, entries, unit.source)
        for mirror in units:
            change_release(pool, mirror, b'Suite: stable\nDate: Tue, 03 Nov 2026 00:00:00 UTC\n')
    with monkeypatch.context() as patches:
        patches.setattr('mmw.core.publisher.manifest.now_stamp', lambda: '20261103T120000Z')
        points.record_points_after_sync(cfg, md, {plain.identity})
    with Pool(md) as pool:
        pool.reconcile_refs(unit.name, [*entries, (checksum, 'pool/amd64.deb', KIND_PKG)], unit.source)
        for mirror in units:
            change_release(pool, mirror, b'Suite: stable\nDate: Wed, 04 Nov 2026 00:00:00 UTC\n')
    with monkeypatch.context() as patches:
        patches.setattr('mmw.core.publisher.manifest.now_stamp', lambda: '20261104T120000Z')
        points.record_points_after_sync(cfg, md, {plain.identity})

    preview = preview_transfer(net, md, cfg=cfg, since='2026-11-02')
    row = next(row for row in preview.rows if row.repo == plain.identity)
    assert row.to_date == '20261104T000000Z'
    bundle = bundle_network(net, cfg, md, out_path=tmp_path / 'range.tar', since='2026-11-02')
    with tarfile.open(bundle.out_path) as tar:
        assert f'blobs/{checksum[:2]}/{checksum}' in tar.getnames()
        assert sum(m.size for m in tar if m.name.startswith('blobs/') and m.isfile()) == preview.bytes
