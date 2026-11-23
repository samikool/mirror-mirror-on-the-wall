"""Retained point checks use the same invariant as point recording."""
from typer.testing import CliRunner
import pytest

from site_apt_rows import Repo
from mmw.cli import app
from mmw.core import config
from mmw.core.pool import Pool
from mmw.site.tracks import PointDraft

KEY = 'https://ex.org/debian'


def _point_pool(tmp_path):
    r = Repo()
    r.deb('pool/alpha.deb')
    r.suite('jammy', '21 Apr 2022 17:16:08 UTC', ['pool/alpha.deb'], compress='')
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / 'mirror'),
                        sources=[config.SourceConfig(url=KEY, suites=['jammy'],
                                                     components=['main'], arches=['amd64'])])
    config.save(cfg, tmp_path)
    return cfg, r, tmp_path


@pytest.fixture
def point_pool(tmp_path):
    return _point_pool(tmp_path)


def record(cfg, r, mixed):
    if mixed:
        r.put('dists/jammy/main/binary-amd64/Packages', b'Package: changed\n\n')
    with Pool(cfg.settings.mirror_dir) as pool:
        for cs, body in r.blobs.items():
            pool.add(body, cs, 'sha256', 'blob', len(body))
        pool.record_point(KEY, PointDraft(r.base + '/dists/jammy', 'release:test',
                                         '20220421T171608Z', {'Suite': 'jammy'}, r.rows), '20220421T180000Z')


@pytest.mark.parametrize('mixed', [False, True])
def test_verify_points(point_pool, mixed):
    cfg, r, folder = point_pool
    record(cfg, r, mixed)
    result = CliRunner().invoke(app, ['--config', str(folder), 'verify', '--points'])
    assert result.exit_code == int(mixed), result.output
    assert ('jammy 2022-04-21: main/binary-amd64/Packages does not match its Release'
            if mixed else 'every kept point is consistent') in result.output


def test_tui_check_points(point_pool):
    import asyncio
    from mmw.tui.app import MmwApp
    from waiting import until, shows
    cfg, r, folder = point_pool
    record(cfg, r, True)

    async def go():
        app = MmwApp(cfg, folder, initial_section='repositories')
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: app.screen.query('#repo-list'))
            assert 'check_points' in app.screen.MORE
            app.screen.query_one('#repo-list').focus()
            await pilot.press('v')
            await until(pilot, lambda: type(app.screen).__name__ == 'CheckPointsPage')
            await shows(pilot, lambda: app.screen.query_one('#points-result').render(), 'does not match its Release')
    asyncio.run(go())


def test_bundle_preview_warns_of_a_mixed_point(tmp_path):
    from mmw.core.repository_points import set_current_repository_points
    from mmw.core.transfer_ranges import preview_transfer
    cfg, r, _ = _point_pool(tmp_path)
    record(cfg, r, True)
    net = config.NetworkConfig(name='lab', nginx=False, bundle_dir=tmp_path / 'usb')
    cfg = cfg.model_copy(update={'networks': [net]})
    units = cfg.sources[0].to_mirrors()
    with Pool(cfg.settings.mirror_dir) as pool:
        [point] = pool.repository_points()
        pool.record_point_units(point.id, KEY, [(u.dims, list(r.rows)) for u in units])
        set_current_repository_points(pool, KEY, [(point.id, True, None)])
    preview = preview_transfer(net, cfg.settings.mirror_dir, cfg=cfg, full=True)
    assert any('does not match its Release' in p for p in preview.warnings), preview

    from mmw.core.publisher.bundle import bundle_network
    from mmw.site import home as H
    from mmw.site.apply import plan_apply
    bundle = bundle_network(net, cfg, cfg.settings.mirror_dir, out_path=tmp_path / 'mixed.tar', full=True)
    assert bundle.out_path.is_file()
    home = H.init_root(tmp_path / 'far', name='main', set_id='s1', network='lab')
    review = plan_apply(home, str(bundle.out_path))
    assert any('does not match its Release' in p for row in review.rows for p in row.problems)
    from mmw.tui.site.apply_flow import ApplyReviewPage
    assert 'does not match its Release' in ApplyReviewPage(review, str(bundle.out_path))._notes(set()).plain


def test_corrupt_retained_index_is_reported_as_unreadable():
    from mmw.core.point_check import check_suite
    r = Repo()
    r.suite('jammy', '21 Apr 2022 17:16:08 UTC', [])
    r.put('dists/jammy/main/binary-amd64/Packages.xz', b'corrupt')
    assert any('unreadable' in p for p in check_suite(r.rows, r.base + '/dists/jammy', r.read))
