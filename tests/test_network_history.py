"""Earlier repository points enter a network only through the explicit action."""

import pytest

from mmw.core import config, points
from mmw.core.pool import Pool
from test_repository_points import apt_repository


def history(tmp_path):
    cfg, md, units = apt_repository(tmp_path)
    points.record_points_after_sync(cfg, md, {units[0].source}, say=lambda _: None)
    with Pool(md) as pool:
        current = pool.repository_points(units[0].source)[0]
        track = pool.track_of_point(current.id)
        from mmw.site.tracks import PointDraft
        import hashlib
        body = b'Suite: stable\nDate: Thu, 01 Oct 2026 00:00:00 UTC\n'
        cs = hashlib.sha256(body).hexdigest()
        pool.add(body, cs, 'sha256', 'Release', len(body))
        files = pool.point_files(current.id)
        previous_release = files[track.name + '/Release']
        files.pop(track.name + '/by-hash/SHA256/' + previous_release, None)
        files[track.name + '/Release'] = cs
        files[track.name + '/by-hash/SHA256/' + cs] = cs
        draft = PointDraft(track.name, 'release:' + cs, '20261001T000000Z', {'Suite': 'stable'}, files)
        old, _ = pool.record_point(units[0].source, draft, '20261002T000000Z')
        pool.record_point_units(old, units[0].source, [(u.dims, draft.files) for u in units])
    net = config.NetworkConfig(name='lab', nginx=False, certs=False)
    cfg = cfg.model_copy(update={'networks': [net]})
    points.record_network_points(net, cfg, md)
    return cfg, old


def test_include_earlier_preserves_current_and_is_idempotent(tmp_path):
    from mmw.commands.network_history import prepare, include
    cfg, old = history(tmp_path)
    with Pool(cfg.settings.mirror_dir) as pool:
        before = points.current_points(pool, 'lab')
    plan = prepare(cfg, 'lab', since='2026-10-01')
    assert [p.id for p in plan.points] == [old]
    assert include(cfg, plan) == 1
    assert include(cfg, prepare(cfg, 'lab')) == 0
    with Pool(cfg.settings.mirror_dir) as pool:
        assert points.current_points(pool, 'lab') == before
        assert old in {p.id for p in pool.network_points('lab')}
        assert pool.network_slice('lab', old)


def test_since_repositories_stubs_and_stale_selection(tmp_path):
    from mmw.commands.network_history import prepare, include
    cfg, old = history(tmp_path)
    assert prepare(cfg, 'lab', since='2026-10-02').points == ()
    with pytest.raises(LookupError):
        prepare(cfg, 'lab', repos=['unknown'])
    with pytest.raises(ValueError):
        prepare(cfg, 'lab', since='banana')
    plan = prepare(cfg, 'lab')
    changed = cfg.model_copy(update={'networks': [replace_network(cfg.networks[0])]})
    with pytest.raises(ValueError, match='Look again'):
        include(changed, plan)
    with Pool(cfg.settings.mirror_dir) as pool:
        pool.stub_point(old)
    plan = prepare(cfg, 'lab')
    assert plan.points == () and any('no longer kept' in s for s in plan.notes)


def replace_network(net):
    return net.model_copy(update={'select': [config.NetworkSelect(source='https://apt.test/debian', arches=['amd64'])]})


def test_network_history_cli(tmp_path):
    from typer.testing import CliRunner
    from mmw.cli import app
    cfg, old = history(tmp_path)
    config.save(cfg, tmp_path)
    args = ['--config', str(tmp_path), 'network', 'history', '-n', 'lab']
    result = CliRunner().invoke(app, [*args, '--dry-run'])
    assert result.exit_code == 0, result.output
    assert 'stable' in result.output and '1' in result.output
    with Pool(cfg.settings.mirror_dir) as pool:
        assert old not in {p.id for p in pool.network_points('lab')}
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert 'Included 1' in result.output


def test_next_history_bundle_lists_included_points(tmp_path):
    from mmw.commands.network_history import prepare, include
    from mmw.core.publisher.bundle import bundle_network
    from test_bundle_mmw import _read
    cfg, old = history(tmp_path)
    net = cfg.networks[0]
    bundle_network(net, cfg, cfg.settings.mirror_dir, out_path=tmp_path / 'before.tar')
    assert include(cfg, prepare(cfg, net.name)) == 1
    result = bundle_network(net, cfg, cfg.settings.mirror_dir, out_path=tmp_path / 'after.tar', history=True)
    header, _, _, carried, _ = _read(result.out_path)
    assert len(header['points'][cfg.sources[0].identity]['points']) == 2
    assert carried  # the earlier Release metadata, without resending shared payloads


def test_layered_network_does_not_include_upstream_history(tmp_path):
    from mmw.commands.network_history import prepare
    cfg, _ = history(tmp_path)
    net = cfg.networks[0].model_copy(update={'layers': [config.NetworkLayer(
        source=cfg.sources[0].identity, signing='unsigned')]})
    cfg = cfg.model_copy(update={'networks': [net]})
    plan = prepare(cfg, net.name)
    assert plan.points == ()
    assert any('layer' in note for note in plan.notes)
