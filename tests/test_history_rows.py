"""One history model (`commands.history.prepare`) for `mmw history`, its
`--json` and the TUI: what each point is to each network and root."""
import json

import pytest
from typer.testing import CliRunner

from mmw.core import config
from mmw.cli import app
from mmw.commands import history
from mmw.commands.bundle import bundle_network
from mmw.core import points
from mmw.core.pool import Pool
from test_transfer_repository_selection import synced, _point  # noqa: F401


@pytest.fixture
def lab(synced, tmp_path):  # noqa: F811
    """Four syncs (01–04 Nov); `pub` publishes as of the second; `oak` was
    bundled up to the third."""
    cfg, net, md, units = synced
    unit = next(u for u in units if u.arch == 'amd64')
    oak = net.model_copy(update={'name': 'oak'})
    cfg = cfg.model_copy(update={'networks': [oak]})
    config.save(cfg, tmp_path)
    _point(cfg, md, unit, 2, True)
    second = [r.point for r in history.prepare(cfg) if r.date[:8] == '20261102']
    with Pool(md) as pool:
        pool.record_published(str(tmp_path / 'pub'), oak.name, 'as-of:20261102', second)
    _point(cfg, md, unit, 3, False)
    bundle_network(oak, cfg, md, out_path=tmp_path / 'oak.tar', full=True, history=True)
    _point(cfg, md, unit, 4, True)
    return cfg, oak, md, unit, tmp_path


def _by_day(rows):
    return {int(r.date[6:8]): r for r in rows}


def test_rows_say_what_each_point_is(lab):
    cfg, oak, md, unit, tmp_path = lab
    rows = _by_day(history.prepare(cfg))
    assert set(rows) == {1, 2, 3, 4}
    assert f'published:{tmp_path / "pub"}' in rows[2].marks
    assert not any(m.startswith('published:') for d in (1, 3, 4) for m in rows[d].marks)
    for d in (1, 2, 3):
        assert 'sent:oak' in rows[d].marks and 'unsent:oak' not in rows[d].marks
    assert 'unsent:oak' in rows[4].marks and 'sent:oak' not in rows[4].marks
    assert 'current' in rows[4].marks and not any('current' in rows[d].marks for d in (1, 2, 3))
    assert rows[4].current and not rows[1].current
    assert all(r.size > 0 and r.label is None and 'drops' not in r.marks for r in rows.values())
    assert all('inconsistent' not in r.marks for r in rows.values())


def test_drops_follow_keep_points(lab):
    cfg, oak, md, unit, tmp_path = lab
    with Pool(md) as pool:              # unpublish, so retention alone decides
        pool.record_published(str(tmp_path / 'pub'), oak.name, 'live', [])
    cfg = cfg.model_copy(update={'settings': cfg.settings.model_copy(update={'keep_points': 2})})
    rows = _by_day(history.prepare(cfg))
    assert {d for d, r in rows.items() if 'drops' in r.marks} == {1, 2}
    with Pool(md, read_only=True) as pool:
        plan = points.retention_plan(pool, cfg.networks, cfg=cfg)
        assert sorted(r.point for r in rows.values() if 'drops' in r.marks) == plan
        assert pool.points_frees(plan) == pool.prune_preview([s.identity for s in cfg.sources], (), plan)[1]
        for r in rows.values():
            assert r.frees == pool.points_frees([r.point])
    assert rows[2].frees > 0                  # pool/amd64.deb of 02 Nov, which 04 Nov replaced


def test_cli_lists_grouped_with_sizes_and_marks(lab):
    cfg, oak, md, unit, tmp_path = lab
    result = CliRunner().invoke(app, ['--config', str(tmp_path), 'history'])
    assert result.exit_code == 0, result.output
    out = result.output
    assert '20261104T' not in out                       # dates, never stamps
    days = [out.index(f'2026-11-0{d}') for d in (4, 3, 2, 1)]
    assert days == sorted(days)                         # newest first
    line = next(x for x in out.splitlines() if '2026-11-04' in x)
    assert 'latest sync' in line and 'not yet sent to oak' in line and ' B' in line
    assert 'sent to oak' in next(x for x in out.splitlines() if '2026-11-01' in x)
    assert f'published at {tmp_path / "pub"}' in next(x for x in out.splitlines() if '2026-11-02' in x)


def test_cli_json(lab):
    cfg, oak, md, unit, tmp_path = lab
    result = CliRunner().invoke(app, ['--config', str(tmp_path), 'history', '--json'])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    rows = history.prepare(cfg)
    assert [d['date'] for d in data] == [r.date for r in rows]
    newest = data[0]
    assert set(newest) >= {'repo', 'suite', 'date', 'ident', 'held', 'size', 'frees', 'marks', 'label'}
    assert 'current' in newest['marks'] and newest['size'] == rows[0].size


def _alias_suite(md, alias):
    """As when Debian moves a suite on: its Release says Suite: `alias` now."""
    with Pool(md) as pool, pool._lock:
        pool._conn.execute('UPDATE tracks SET suite = ?', (alias,))
        pool._conn.commit()


def test_suite_named_by_directory_with_its_alias(lab):
    cfg, oak, md, unit, tmp_path = lab
    _alias_suite(md, 'oldstable')
    rows = history.prepare(cfg)
    assert {(r.suite, r.alias, r.heading) for r in rows} == {('stable', 'oldstable', 'stable (oldstable)')}
    out = CliRunner().invoke(app, ['--config', str(tmp_path), 'history']).output
    assert '  stable (oldstable)  2026-11-04' in out
    assert 'stable 04 Nov · last sync ' in out           # the summary names the directory
    with Pool(md, read_only=True) as pool:           # the alias still picks the suite
        assert history.delete_plan(pool, cfg, rows[0].repo, '2026-11-01', suite='oldstable')


def test_cli_says_what_the_dates_are(lab):
    cfg, oak, md, unit, tmp_path = lab
    out = CliRunner().invoke(app, ['--config', str(tmp_path), 'history']).output
    assert out.rstrip().endswith(history.DATES_NOTE)
    assert set(history.last_synced(cfg)) == {r.repo for r in history.prepare(cfg)}
