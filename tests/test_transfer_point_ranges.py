"""Retained points, with independent suite dates, are transfer endpoints."""
import json
import tarfile

import pytest
from typer.testing import CliRunner

from mmw.cli import app
from mmw.core import config, points
from mmw.core.pool import Pool
from mmw.core.publisher.bundle import bundle_network
from test_bundle_format3 import mmw_net  # noqa: F401
from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply_timeline import set_a
from test_snapshot_repo_versions import _key


def header(path):
    with tarfile.open(path) as tar:
        return json.loads(tar.extractfile('.mmw/manifest.jsonl').readline())


def record(env, *days):
    cfg, net, md = env
    for day in days:
        set_a(md, day)
        points.record_network_points(net, cfg, md, stamp=f'202611{day:02}T120000Z')


def dates(h, cfg):
    return sorted(p['date'][:8] for p in h['points'][_key(cfg, 'a')]['points'])


def test_until_selects_retained_points_and_bounds_history(mmw_net, tmp_path):  # noqa: F811
    cfg, net, md = mmw_net
    record(mmw_net, 1, 2, 3, 4)
    with Pool(md, read_only=True) as pool:
        before = pool.snapshots(net.name)
    result = bundle_network(net, cfg, md, out_path=tmp_path / 'b.tar', until='2026-11-03', history=True)
    h = header(result.out_path)
    assert dates(h, cfg) == ['20261101', '20261102', '20261103']
    assert h['format'] == 3 and h['selection'] is True
    assert h['created'] != '20261103T235959Z'
    with Pool(md, read_only=True) as pool:
        assert pool.snapshots(net.name) == before


def test_until_missing_selected_track_refuses_instead_of_dropping_it(mmw_net, tmp_path):  # noqa: F811
    cfg, net, md = mmw_net
    record(mmw_net, 2)
    with pytest.raises(ValueError, match='no point.*at or before'):
        bundle_network(net, cfg, md, out_path=tmp_path / 'b.tar', until='2000-01-01')
    assert not (tmp_path / 'b.tar').exists()


def test_until_cannot_be_combined_with_legacy_snapshot_target(mmw_net, tmp_path):  # noqa: F811
    cfg, net, md = mmw_net
    with pytest.raises(ValueError, match='--until.*--to'):
        bundle_network(net, cfg, md, out_path=tmp_path / 'b.tar', until='2026-11-03', to_ref='old')


def test_cli_until_uses_point_target(mmw_net, tmp_path):  # noqa: F811
    cfg, net, md = mmw_net
    record(mmw_net, 1, 2, 3)
    config.save(cfg, tmp_path)
    result = CliRunner().invoke(app, ['--config', str(tmp_path), 'bundle', '-n', net.name,
                                      '--out', str(tmp_path / 'cli.tar'), '--until', '2026-11-02', '--full'])
    assert result.exit_code == 0, result.output
    assert dates(header(tmp_path / 'cli.tar'), cfg) == ['20261102']


def test_preview_reports_actual_dates_per_track_without_recording(mmw_net):  # noqa: F811
    from mmw.commands.bundle import preview_transfer
    cfg, net, md = mmw_net
    record(mmw_net, 1, 2, 3)
    with Pool(md, read_only=True) as pool:
        before = pool.bundles(net.name), pool.snapshots(net.name)
    preview = preview_transfer(net, md, until='2026-11-02', full=True)
    row = next(r for r in preview.rows if r.repo == _key(cfg, 'a'))
    assert row.to_date.startswith('20261102') and row.points == 1
    assert row.from_date is None
    assert preview.blobs > 0 and preview.bytes > 0
    with Pool(md, read_only=True) as pool:
        assert (pool.bundles(net.name), pool.snapshots(net.name)) == before


def test_tui_until_previews_and_transfers_without_snapshot_choices(mmw_net, tmp_path):  # noqa: F811
    from textual.widgets import Input, Select
    from test_snapshot_tui import _run, _finished
    from pickers import confirm_picker
    from waiting import until, shows
    cfg, net, md = mmw_net
    net = net.model_copy(update={'bundle_dir': tmp_path / 'usb'})
    cfg = cfg.model_copy(update={'networks': [net]})
    record((cfg, net, md), 1, 2, 3)

    async def check(app, pilot):
        await pilot.press('t')
        await until(pilot, lambda: type(app.screen).__name__ == 'BundlePickPage' and app.screen.query('#pick-to'))
        app.screen.query_one('#pick-to', Select).value = 'date'
        app.screen.query_one('#pick-until', Input).value = '2026-11-02'
        app.screen.query_one('#pick-installer', Select).value = 'no'
        await shows(pilot, lambda: app.screen.query_one('#pick-estimate').render(), '2026-11-02', 'blobs')
        await confirm_picker(app, pilot, 'BundlePickPage')
        await until(pilot, lambda: _finished(app))
        assert app.active_transfer.outcome == 'ok', app.notes
    _run(cfg, md.parent, check)
    bundles = list(net.bundle_dir.glob('*.tar'))
    assert len(bundles) == 1
    assert dates(header(bundles[0]), cfg) == ['20261102']
    with Pool(md, read_only=True) as pool:
        assert pool.snapshots(net.name) == []


def test_date_bundle_applies_via_cli_without_rollback_or_point_removal(mmw_net, tmp_path, monkeypatch):  # noqa: F811
    from mmw.site.db import SiteDB
    from mmw.site.home import read_root
    from test_site_apply_timeline import _served_day, _track
    cfg, net, md = mmw_net
    record(mmw_net, 1, 2, 3, 4)
    newer = bundle_network(net, cfg, md, out_path=tmp_path / 'newer.tar', full=True)
    older = bundle_network(net, cfg, md, out_path=tmp_path / 'older.tar', full=True, until='2026-11-03', history=True)
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    root = tmp_path / 'site'
    for path in (newer.out_path, older.out_path):
        result = CliRunner().invoke(app, ['site', 'apply', str(path), '--root', f'*={root}', '--yes'])
        assert result.exit_code == 0, result.output
    with SiteDB(read_root(str(root)).home) as db:
        assert _served_day(db, _key(cfg, 'a')) == '20261104'
        assert {p.date[:8] for p in db.points(_track(db, _key(cfg, 'a')).id)} == {'20261101', '20261102', '20261103', '20261104'}
    h = header(older.out_path)
    assert len({p['date'] for meta in h['points'].values() for p in meta['points']}) > 1


def test_site_review_shows_each_arriving_suite_date(mmw_net, tmp_path):  # noqa: F811
    import asyncio
    from textual.widgets import SelectionList
    from mmw.site.apply import plan_apply
    from mmw.tui.site.apply_flow import ApplyReviewPage
    from mmw.tui.ui.base import ThemedApp
    from waiting import settle
    from test_site_apply_timeline import _apply
    from mmw.site import home as H
    cfg, net, md = mmw_net
    record(mmw_net, 1, 2, 3)
    path = bundle_network(net, cfg, md, out_path=tmp_path / 'b.tar', until='2026-11-02', full=True, history=True).out_path
    home = H.init_root(tmp_path / 'root', name='main', set_id='s1', network=net.name)
    _apply(home, str(path))
    plan = plan_apply(home, str(path))
    row = next(r for r in plan.rows if r.repo == _key(cfg, 'a'))
    assert {d[:8] for suite, d in row.point_dates} == {'20261101', '20261102'}

    class Host(ThemedApp):
        def on_mount(self):
            self.push_screen(ApplyReviewPage(plan, str(path)))

    async def scenario():
        app = Host()
        async with app.run_test(size=(140, 45)) as pilot:
            await settle(pilot)
            choices = app.screen.query_one('#review-repos', SelectionList)
            rendered = ' '.join(str(choices.get_option_at_index(i).prompt) for i in range(choices.option_count))
            assert '2026-11-01' in rendered and '2026-11-02' in rendered
            assert 'rollback' in app.screen.HELP.lower()
    asyncio.run(scenario())


@pytest.mark.parametrize('verb', ['bundle', 'stage'])
def test_cli_dry_run_previews_range_without_writing_bundle(mmw_net, tmp_path, verb):  # noqa: F811
    cfg, net, md = mmw_net
    record(mmw_net, 1, 2, 3)
    config.save(cfg, tmp_path)
    result = CliRunner().invoke(app, ['--config', str(tmp_path), verb, '-n', net.name,
                                      '--until', '2026-11-02', '--full', '--dry-run'])
    assert result.exit_code == 0, result.output
    assert '2026-11-02' in result.output and 'new blobs' in result.output
    with Pool(md, read_only=True) as pool:
        assert pool.bundles(net.name) == []


def test_explicit_pruned_target_does_not_silently_choose_an_older_point(mmw_net, tmp_path):  # noqa: F811
    cfg, net, md = mmw_net
    record(mmw_net, 1, 2, 3)
    with Pool(md) as pool:
        for track in pool.network_tracks(net.name):
            if track.repo == _key(cfg, 'a'):
                for p in pool.network_points(net.name, track.id):
                    if p.date.startswith('20261102'):
                        pool.stub_point(p.id)
    with pytest.raises(ValueError, match='no longer kept'):
        bundle_network(net, cfg, md, out_path=tmp_path / 'b.tar', until='2026-11-02')


def test_history_skips_pruned_intermediate_points_with_a_warning(mmw_net, tmp_path):  # noqa: F811
    cfg, net, md = mmw_net
    record(mmw_net, 1, 2, 3)
    with Pool(md) as pool:
        for track in pool.network_tracks(net.name):
            if track.repo == _key(cfg, 'a'):
                for p in pool.network_points(net.name, track.id):
                    if p.date.startswith('20261102'):
                        pool.stub_point(p.id)
    result = bundle_network(net, cfg, md, out_path=tmp_path / 'b.tar', history=True)
    assert dates(header(result.out_path), cfg) == ['20261101', '20261103']
    assert any('no longer kept' in w for w in result.warnings)


def test_historical_target_does_not_walk_or_record_live_state(mmw_net, tmp_path, monkeypatch):  # noqa: F811
    cfg, net, md = mmw_net
    record(mmw_net, 1, 2)

    def forbidden(*args, **kwargs):
        raise AssertionError('a retained target must not walk live refs')

    monkeypatch.setattr(points, 'record_network_points', forbidden)
    result = bundle_network(net, cfg, md, out_path=tmp_path / 'b.tar', until='2026-11-01')
    assert dates(header(result.out_path), cfg) == ['20261101']


def test_until_uses_selected_arch_date_not_repository_other_arch_date(tmp_path):
    from test_repository_points import apt_repository, change_release
    cfg, md, units = apt_repository(tmp_path)
    with Pool(md) as pool:
        change_release(pool, units[1], b'Suite: stable\nDate: Tue, 03 Nov 2026 00:00:00 UTC\n')
    points.record_points_after_sync(cfg, md, {units[0].source}, say=lambda _: None)
    net = config.NetworkConfig(name='amd', nginx=False, certs=False,
                               select=[config.NetworkSelect(source=units[0].source, arches=['amd64'])])
    cfg = cfg.model_copy(update={'networks': [net]})
    points.record_network_points(net, cfg, md, stamp='20261104T000000Z')
    result = bundle_network(net, cfg, md, out_path=tmp_path / 'b.tar', until='2026-11-01')
    h = header(result.out_path)
    assert [p['date'][:8] for p in h['points'][units[0].source]['points']] == ['20261101']


def test_until_resolves_two_suites_of_one_repository_independently(mmw_net, tmp_path):  # noqa: F811
    import hashlib
    from pathlib import Path
    from test_bundle_ranges_review import _with_old_suite
    from test_site_apply_timeline import DATES
    cfg, net, md = mmw_net
    _with_old_suite(md, True)
    points.record_network_points(net, cfg, md, stamp='20261101T120000Z')
    with Pool(md) as pool:
        refs = []
        for path, blob, _size, kind, checksum in pool.mirror_files(['a']):
            day = {'dists/stable/Release': 3, 'dists/old/Release': 2}.get(path)
            if day:
                body = Path(blob).read_bytes().replace(DATES[1].encode(), DATES[day].encode())
                checksum = hashlib.sha256(body).hexdigest()
                pool.add(body, checksum, 'sha256', 'Release', len(body))
            refs.append((checksum, path, kind))
        pool.reconcile_refs('a', refs)
    points.record_network_points(net, cfg, md, stamp='20261103T120000Z')
    result = bundle_network(net, cfg, md, out_path=tmp_path / 'b.tar', until='2026-11-02')
    h = header(result.out_path)
    actual = {p['fields']['Suite']: p['date'][:8] for p in h['points'][_key(cfg, 'a')]['points']}
    assert actual == {'stable': '20261101', 'old': '20261102'}


def test_next_history_transfer_keeps_unsent_points_beyond_older_target(mmw_net, tmp_path, monkeypatch):  # noqa: F811
    from mmw.core import snapshots
    cfg, net, md = mmw_net
    record(mmw_net, 1, 2, 3, 4)
    monkeypatch.setattr(snapshots, 'now_stamp', lambda: '20261118T120000Z')
    bundle_network(net, cfg, md, out_path=tmp_path / 'old.tar', until='2026-11-02', history=True)
    with Pool(md, read_only=True) as pool:
        doomed = set(points.retention_plan(pool, [net.model_copy(update={'keep_points': 1})]))
        third = {p.id for t in pool.network_tracks(net.name) if t.repo == _key(cfg, 'a')
                 for p in pool.network_points(net.name, t.id) if p.date.startswith('20261103')}
        assert third and not third & doomed
    result = bundle_network(net, cfg, md, out_path=tmp_path / 'next.tar', history=True)
    assert dates(header(result.out_path), cfg) == ['20261103', '20261104']
