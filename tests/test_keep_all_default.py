"""History is kept in full unless a retention rule says otherwise, on both
sides; networks have no retention of their own any more."""
import pytest
from textual.widgets import ListView
from typer.testing import CliRunner

from mmw.core import config, points
from mmw.core.config import tomllib
from mmw.core.pool import Pool
from mmw.site import home as H
from mmw.site import ops
from mmw.site.tracks import PointDraft
from mmw.tui.network_screens import EditNetworkPage
from test_site_retention import _days, _own_state  # noqa: F401
from test_site_apply_timeline import env, set_a  # noqa: F401
from test_dry_run_totals import two_mirrors  # noqa: F401
from test_tui_networks_page import _cfg, _names, _run, _settle
from waiting import until


def _four_syncs(tmp_path, **settings):
    src = config.SourceConfig(url='https://apt.test/debian')
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path, **settings), sources=[src])
    with Pool(tmp_path) as pool:
        for day in range(1, 5):
            draft = PointDraft('apt.test/debian/dists/stable', f'release:{day}', f'2026110{day}T000000Z',
                               {'Suite': 'stable'}, {'path': f'blob{day}'})
            pool.record_point(src.identity, draft, f'2026110{day}T120000Z')
    return cfg


def test_default_keeps_every_point(tmp_path):
    cfg = _four_syncs(tmp_path)
    assert cfg.settings.keep_points == 'all'
    with Pool(tmp_path) as pool:
        assert points.apply_retention(pool, [], cfg=cfg) == 0
        assert all(p.held for p in pool.repository_points(cfg.sources[0].identity))
    config.save(cfg, tmp_path)
    assert 'keep_points' not in (tmp_path / 'mmw.toml').read_text()


def test_explicit_three_still_prunes(tmp_path):
    cfg = _four_syncs(tmp_path, keep_points=3)
    config.save(cfg, tmp_path)
    assert 'keep_points = 3' in (tmp_path / 'mmw.toml').read_text()
    loaded = config.load(tmp_path)
    with Pool(tmp_path) as pool:
        assert points.apply_retention(pool, [], cfg=loaded) == 1


def test_old_network_keep_points_loads_with_a_warning(tmp_path, capsys):
    (tmp_path / 'mmw.toml').write_text(
        f'[settings]\nmirror_dir = "{tmp_path / "m"}"\n\n'
        '[[networks]]\nname = "oak"\nkeep_points = 5\nkeep_suites = { sid = 1 }\n')
    cfg = config.load(tmp_path)
    config.load(tmp_path)
    err = capsys.readouterr().err
    assert err.count('network oak: keep_points') == 1
    assert 'no longer used — history is kept per repository (`mmw keep`)' in err
    assert not hasattr(cfg.networks[0], 'keep_points')
    config.save(cfg, tmp_path)
    saved, = tomllib.loads((tmp_path / 'mmw.toml').read_text())['networks']
    assert 'keep_points' not in saved and 'keep_suites' not in saved


def test_network_keep_fields_are_refused_in_code():
    with pytest.raises(ValueError):
        config.NetworkConfig.model_validate({'name': 'oak', 'nope': 1})
    net = config.NetworkConfig.model_validate({'name': 'oak', 'keep_points': 5})
    assert 'keep_points' not in net.model_dump()


def test_a_networks_own_points_follow_the_same_rules(tmp_path):
    # A network's own points (scoped: a walk of a repository no sync recorded,
    # or converted from an older mmw) follow their repository's rule; the
    # network's own track (keys, pages) the global one. No network pins them.
    src = config.SourceConfig(url='https://apt.test/debian')
    net = config.NetworkConfig(name='oak')
    with Pool(tmp_path) as pool:
        for day in range(1, 5):
            stamp = f'2026110{day}T120000Z'
            draft = PointDraft('apt.test/debian/dists/stable', f'release:{day}', f'2026110{day}T000000Z',
                               {'Suite': 'stable'}, {'path': f'blob{day}'})
            pid, _ = pool.record_point(src.identity, draft, stamp, scope=net.name)
            pool.link_network_point(net.name, pid, stamp)
            own = PointDraft('network', f'net:{day}', stamp, {}, {'keys/k.asc': f'key{day}'})
            pid, _ = pool.record_point('network:oak', own, stamp, scope=net.name)
            pool.link_network_point(net.name, pid, stamp)
        for keep, gone in (('all', 0), (1, 6)):
            cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path, keep_points=keep),
                                sources=[src], networks=[net])
            assert len(points.retention_plan(pool, [net], cfg=cfg)) == gone
        assert len(points.retention_plan(pool, [], cfg=cfg)) == 8      # a removed network keeps nothing


def test_site_default_keeps_all(env, tmp_path):  # noqa: F811
    cfg, home = _days(env, range(1, 6), tmp_path)
    assert H.load_site(home).keep == 'all'
    assert ops.clean_plan_here(home) == []
    assert 'keep' not in tomllib.loads((home / H.SETTINGS).read_text())


def test_site_explicit_three_still_cleans(env, tmp_path):  # noqa: F811
    cfg, home = _days(env, range(1, 6), tmp_path)
    ops.keep_here(home, 3)
    assert H.load_site(home).keep == 3
    assert len(ops.clean_plan_here(home)) == 2


def test_costs_have_no_network_rows(tmp_path):
    from mmw.commands.prune import costs_of_keeping
    cfg = _four_syncs(tmp_path)
    cfg = cfg.model_copy(update={'networks': [config.NetworkConfig(name='oak')]})
    table = costs_of_keeping(cfg, tmp_path)
    assert list(table) == [f'repository {cfg.sources[0].name}']
    config.save(cfg, tmp_path)
    from mmw.cli import app
    r = CliRunner().invoke(app, ['--config', str(tmp_path), 'prune', '--costs'])
    assert r.exit_code == 0, r.output
    assert '    oak ' not in r.output and f'repository {cfg.sources[0].name}' in r.output


def test_network_editor_has_no_keep_fields(tmp_path, monkeypatch):
    async def check(app, pilot):
        await until(pilot, lambda: app.screen.query("#net-list") and "lab" in _names(app))
        app.screen.query_one("#net-list", ListView).index = _names(app).index("lab")
        await _settle(pilot)
        await pilot.press("e")
        await until(pilot, lambda: isinstance(app.screen, EditNetworkPage) and app.screen.query("#btn-save"))
        assert not app.screen.query("#inp-keep-points") and not app.screen.query("#inp-keep-suites")
    _run(tmp_path, check, cfg=_cfg(tmp_path, "lab"), monkeypatch=monkeypatch)
