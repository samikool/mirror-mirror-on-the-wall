"""Retention ages the state from when its successor arrived."""
import asyncio

from typer.testing import CliRunner

from mmw.cli import app
from mmw.core import config, points
from mmw.core.pool import Pool
from mmw.core.repository_points import set_current_repository_points
from mmw.site.tracks import PointDraft

KEY = 'https://example.org/debian'


def lab(tmp_path, successor='20261120T000000Z'):
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / 'mirror', keep_points='30d'),
                        sources=[config.SourceConfig(url=KEY)])
    with Pool(cfg.settings.mirror_dir) as pool:
        for stamp in ('20260101T000000Z', successor):
            draft = PointDraft('example.org/debian/dists/stable', 'release:' + stamp, stamp,
                               {'Suite': 'stable'}, {'example.org/debian/dists/stable/Release': stamp})
            pid, new = pool.record_point(KEY, draft, stamp)
        set_current_repository_points(pool, KEY, [(pid, new, draft)])
    return cfg


def test_age_counts_from_when_a_point_was_last_current(tmp_path):
    cfg = lab(tmp_path)
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        assert not points.retention_plan(pool, [], now='20261121T000000Z', cfg=cfg)


def test_age_drops_what_was_replaced_long_ago(tmp_path):
    cfg = lab(tmp_path, '20260920T000000Z')
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        gone = points.retention_plan(pool, [], now='20261121T000000Z', cfg=cfg)
        assert len(gone) == 1
        assert pool.points_of(gone)[gone[0]].stamp == '20260101T000000Z'


def removed_source(tmp_path):
    cfg = lab(tmp_path)
    return cfg.model_copy(update={'sources': [config.SourceConfig(url='https://elsewhere.org/repo')]})


def test_prune_names_a_repository_losing_its_whole_history(tmp_path):
    cfg = removed_source(tmp_path)
    config.save(cfg, tmp_path)
    result = CliRunner().invoke(app, ['--config', str(tmp_path), 'prune', '--dry-run'])
    assert result.exit_code == 0, result.output
    assert f'{KEY}: every point (2) goes — its source is no longer configured' in result.output


def test_tui_prune_names_a_repository_losing_its_whole_history(tmp_path):
    from mmw.tui.app import MmwApp
    from waiting import until, shows
    cfg = removed_source(tmp_path)
    config.save(cfg, tmp_path)

    async def go():
        app = MmwApp(cfg, tmp_path, initial_section='repositories')
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: app.screen.query('#btn-prune-pool'))
            await pilot.click('#btn-prune-pool')
            await until(pilot, lambda: type(app.screen).__name__ == 'ConfirmDialog')
            await shows(pilot, lambda: '\n'.join(str(w.render()) for w in app.screen.query('Static')),
                        f'{KEY}: every point (2) goes — its source is no longer configured')
    asyncio.run(go())
