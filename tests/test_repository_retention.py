"""Repository settings preserve history even when no network selects it."""
import pytest

from mmw.core import config, points
from mmw.core.pool import Pool
from mmw.site.tracks import PointDraft


def seeded(tmp_path, *, keep_points=2, days=5):
    src = config.SourceConfig(url='https://apt.test/debian', keep_points=keep_points)
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path), sources=[src])
    with Pool(tmp_path) as pool:
        for day in range(1, days + 1):
            draft = PointDraft('apt.test/debian/dists/stable', f'release:{day}', f'2026110{day}T000000Z',
                               {'Suite': 'stable'}, {'path': f'blob{day}'})
            pool.record_point(src.identity, draft, f'2026110{day}T120000Z')
    return cfg


@pytest.fixture
def history_network(tmp_path):
    cfg = seeded(tmp_path, keep_points=1, days=6)
    net = config.NetworkConfig(name='oak', history=True)
    with Pool(tmp_path) as pool:
        ids = []
        for point in reversed(pool.repository_points(cfg.sources[0].identity)):
            pool.link_network_point(net.name, point.id, point.stamp)
            ids.append(point.id)
        yield cfg, net, pool, ids


def test_unsent_repository_points_survive_retention(history_network):
    cfg, net, pool, ids = history_network
    pool.record_bundle(net.name, None, '20261102T130000Z', 'oak.tar', 0, 0, 0)
    assert points.retention_plan(pool, [net], cfg=cfg) == ids[:2]


def test_unsent_protection_needs_history(history_network):
    cfg, net, pool, ids = history_network
    net.history = False
    pool.record_bundle(net.name, None, '20261102T130000Z', 'oak.tar', 0, 0, 0)
    assert points.retention_plan(pool, [net], cfg=cfg) == ids[:5]


def test_never_bundled_network_pins_nothing(history_network):
    cfg, net, pool, ids = history_network
    assert points.retention_plan(pool, [net], cfg=cfg) == ids[:5]


def test_sent_points_are_released(history_network):
    cfg, net, pool, ids = history_network
    pool.record_bundle(net.name, None, '20261102T130000Z', 'oak.tar', 0, 0, 0)
    pool.record_sent(net.name, ids[2:5], [], '20261106T130000Z')
    assert points.retention_plan(pool, [net], cfg=cfg) == ids[:5]


def test_repository_retention_without_networks(tmp_path):
    cfg = seeded(tmp_path)
    with Pool(tmp_path) as pool:
        gone = points.retention_plan(pool, [], cfg=cfg)
        assert len(gone) == 3
        assert points.apply_retention(pool, [], cfg=cfg) == 3
        assert len(pool.repository_points(cfg.sources[0].identity)) == 2


def test_network_rules_do_not_override_repository_defaults(tmp_path):
    cfg = seeded(tmp_path)
    cfg.sources[0].keep_points = None
    with Pool(tmp_path) as pool:
        assert len(points.retention_plan(pool, [], cfg=cfg)) == 2
        net = config.NetworkConfig(name='all', keep_points='all')
        for p in pool.repository_points(cfg.sources[0].identity):
            pool.link_network_point(net.name, p.id, p.stamp)
        assert len(points.retention_plan(pool, [net], cfg=cfg)) == 2


def test_keep_settings_roundtrip(tmp_path):
    cfg = seeded(tmp_path)
    cfg.settings.keep_points = 4
    cfg.sources[0].keep_suites = {'stable': 'all'}
    config.save(cfg, tmp_path)
    loaded = config.load(tmp_path)
    assert loaded.settings.keep_points == 4
    assert loaded.sources[0].keep_points == 2
    assert loaded.sources[0].keep_suites == {'stable': 'all'}
    with Pool(tmp_path) as pool:
        assert points.retention_plan(pool, [], cfg=loaded) == []


@pytest.mark.parametrize('value', [-1, True, 'bogus'])
def test_bad_repository_keep_rejected(value):
    with pytest.raises(ValueError):
        config.SourceConfig(url='https://apt.test/debian', keep_points=value)
    with pytest.raises(ValueError):
        config.Settings(mirror_dir='mirror', keep_points=value)


def test_keep_cli_and_costs_without_network(tmp_path):
    from typer.testing import CliRunner
    from mmw.cli import app
    cfg = seeded(tmp_path)
    config.save(cfg, tmp_path)
    r = CliRunner().invoke(app, ['--config', str(tmp_path), 'keep', 'all', '--repo', cfg.sources[0].name])
    assert r.exit_code == 0, r.output
    assert config.load(tmp_path).sources[0].keep_points == 'all'
    r = CliRunner().invoke(app, ['--config', str(tmp_path), 'prune', '--costs'])
    assert r.exit_code == 0, r.output
    assert 'repository' in r.output.lower() and cfg.sources[0].name in r.output
