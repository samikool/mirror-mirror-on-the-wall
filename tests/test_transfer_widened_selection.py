"""A network that adds an architecture: the next transfer carries the diff
for what was already sent and the whole of what is new.

The coworker's case: Ubuntu went over with amd64 only. Later the network
also selects i386 (arm64 here). The next bundle sends only amd64's changes
since the last transfer, but every i386 file, and the far side then serves
both."""
import hashlib
import json
import tarfile

import pytest

from mmw.core import config, points
from mmw.core.pool import KIND_META, KIND_PKG, Pool
from mmw.core.publisher.bundle import bundle_network
from mmw.core.transfer_ranges import preview_transfer
from mmw.site import home as H
from mmw.site.apply import apply_bundle
from test_repository_points import change_release
from test_transfer_repository_selection import synced  # noqa: F401


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _day_two(cfg, md, units):
    """amd64 gains pool/new.deb; the Release moves to 2 Nov for every unit."""
    amd = next(u for u in units if u.arch == 'amd64')
    with Pool(md) as pool:
        entries = [(cs, path, kind) for path, _, _, kind, cs in pool.mirror_files([amd.name])
                   if not path.endswith('/Packages')]
        index = ''.join(f'Package: {n}\nFilename: pool/{n}.deb\n\n'
                        for n in ('amd64', 'shared', 'new')).encode()
        pool.add(index, sha(index), 'sha256', 'Packages', len(index))
        pool.add(b'new', sha(b'new'), 'sha256', 'new.deb', 3)
        entries += [(sha(index), 'dists/stable/main/binary-amd64/Packages', KIND_META),
                    (sha(b'new'), 'pool/new.deb', KIND_PKG)]
        pool.reconcile_refs(amd.name, entries, amd.source)
        for unit in units:
            change_release(pool, unit, b'Suite: stable\nDate: Mon, 02 Nov 2026 00:00:00 UTC\n')
    points.record_points_after_sync(cfg.model_copy(update={'networks': []}), md,
                                    {amd.source}, say=lambda _: None)


def _blobs(path):
    with tarfile.open(path) as tar:
        rows = [json.loads(ln) for ln in
                tar.extractfile('.mmw/manifest.jsonl').read().decode().splitlines()]
        return {m.name.rsplit('/', 1)[-1] for m in tar if m.name.startswith('blobs/')}, rows


@pytest.mark.parametrize('since', [None, '2026-11-01'])
def test_an_added_arch_goes_whole_while_the_sent_one_goes_as_a_diff(synced, tmp_path, since):  # noqa: F811
    cfg, net, md, units = synced
    first = bundle_network(net, cfg, md, out_path=tmp_path / 'amd64-only.tar').out_path
    sent, _ = _blobs(first)
    assert sha(b'amd64') in sent and sha(b'arm64') not in sent
    home = H.init_root(tmp_path / 'far', name='main', set_id='work', network=net.name)
    assert not apply_bundle(home, str(first), say=lambda *_: None).refused

    _day_two(cfg, md, units)
    wide = net.model_copy(update={'select': [config.NetworkSelect(source=units[0].source,
                                                                  arches=['amd64', 'arm64'])]})
    cfg = cfg.model_copy(update={'networks': [wide]})

    plan = preview_transfer(wide, md, cfg=cfg, since=since)
    assert plan.rows[0].from_date is not None          # a diff, not a full transfer
    second = bundle_network(wide, cfg, md, since=since, out_path=tmp_path / 'next.tar').out_path
    carried, _ = _blobs(second)
    # amd64: only what changed since the last transfer.
    assert sha(b'new') in carried
    assert sha(b'amd64') not in carried and sha(b'shared') not in carried
    # arm64: all of it, though its package predates the last transfer.
    assert sha(b'arm64') in carried

    applied = apply_bundle(home, str(second), say=lambda *_: None)
    assert not applied.refused and not applied.incomplete
    far = tmp_path / 'far/apt.test/debian'
    for name, body in (('amd64', b'amd64'), ('arm64', b'arm64'), ('new', b'new'), ('shared', b'shared')):
        assert (far / f'pool/{name}.deb').read_bytes() == body
    assert (far / 'dists/stable/main/binary-arm64/Packages').is_file()
    assert b'new.deb' in (far / 'dists/stable/main/binary-amd64/Packages').read_bytes()


# ── A wiped connected side: no record of the last transfer ──────────────────
#
# What the far side holds then comes from --since DATE alone, and today's
# selection (amd64 + arm64) is not what was sent (amd64 only). A kept bundle
# says what was; without one, --whole says what the far side never had.

@pytest.fixture
def wiped(synced, tmp_path, monkeypatch):  # noqa: F811
    """Old side: amd64 bundled on 1 Nov, kept. New side: resynced from scratch
    with the 2 Nov point, its network now selecting arm64 too."""
    from mmw.core import snapshots
    from test_repository_points import apt_repository
    cfg, net, md, units = synced
    kept = tmp_path / 'kept'
    kept.mkdir()
    monkeypatch.setattr(snapshots, 'now_stamp', lambda: '20261101T180000Z')
    bundle_network(net, cfg, md, out_path=kept / 'amd64-only.tar')
    monkeypatch.undo()
    fresh = tmp_path / 'fresh'
    cfg, md, units = apt_repository(fresh)
    points.record_points_after_sync(cfg, md, {units[0].source}, say=lambda _: None)
    _day_two(cfg, md, units)
    wide = config.NetworkConfig(name='work', nginx=False, certs=False, bundle_dir=fresh / 'usb',
                                select=[config.NetworkSelect(source=units[0].source, arches=['amd64', 'arm64'])])
    cfg = cfg.model_copy(update={'networks': [wide]})
    config.save(cfg, fresh)
    return cfg, wide, md, kept


def _carries(path):
    blobs, _ = _blobs(path)
    return {name for name in ('amd64', 'arm64', 'new', 'shared') if sha(name.encode()) in blobs}


def test_a_kept_bundle_says_what_was_sent(wiped, tmp_path):
    cfg, net, md, kept = wiped
    plan = preview_transfer(net, md, cfg=cfg, since='2026-11-01', kept_dirs=[kept])
    assert plan.whole == ("apt: arches arm64 sent whole (not in amd64-only.tar's selection)",)
    out = bundle_network(net, cfg, md, since='2026-11-01', kept_dirs=[kept], out_path=tmp_path / 'b.tar')
    assert _carries(out.out_path) == {'arm64', 'new'}
    assert out.whole == list(plan.whole)


@pytest.mark.parametrize('spec', ['apt:arches=arm64', 'arches=arm64', 'https://apt.test/debian:arch=arm64'])
def test_whole_sends_what_the_far_side_never_had(wiped, tmp_path, spec):
    cfg, net, md, _ = wiped
    plan = preview_transfer(net, md, cfg=cfg, since='2026-11-01', whole=[spec])
    assert plan.whole == ('apt: arches arm64 sent whole (--whole)',)
    out = bundle_network(net, cfg, md, since='2026-11-01', whole=[spec], out_path=tmp_path / 'b.tar')
    # amd64 as a diff from 1 Nov; arm64 whole, but not what amd64 already carried.
    assert _carries(out.out_path) == {'arm64', 'new'}


def test_whole_also_works_from_the_last_transfer(synced, tmp_path):  # noqa: F811
    cfg, net, md, units = synced
    both = net.model_copy(update={'select': [config.NetworkSelect(source=units[0].source,
                                                                  arches=['amd64', 'arm64'])]})
    cfg = cfg.model_copy(update={'networks': [both]})
    bundle_network(both, cfg, md, out_path=tmp_path / 'first.tar')
    _day_two(cfg, md, units)
    out = bundle_network(both, cfg, md, whole=['arches=arm64'], out_path=tmp_path / 'b.tar')
    assert _carries(out.out_path) == {'arm64', 'new'}


@pytest.mark.parametrize('spec, says', [('apt:arches=i386', 'matches nothing'),
                                        ('nope:arches=arm64', 'unknown repository'),
                                        ('apt:flavour=x', 'expected')])
def test_a_whole_that_names_nothing_is_said(wiped, spec, says):
    cfg, net, md, _ = wiped
    with pytest.raises((ValueError, LookupError), match=says):
        preview_transfer(net, md, cfg=cfg, since='2026-11-01', whole=[spec])


@pytest.mark.parametrize('verb', ['bundle', 'stage'])
def test_cli_whole(wiped, verb):
    from typer.testing import CliRunner

    from mmw.cli import app
    cfg, net, md, _ = wiped
    where = ['--config', str(md.parent), verb, '-n', 'work', '--since', '2026-11-01']
    result = CliRunner().invoke(app, [*where, '--whole', 'apt:arches=arm64', '--dry-run'])
    assert result.exit_code == 0, result.output
    assert 'apt: arches arm64 sent whole (--whole)' in result.output
    result = CliRunner().invoke(app, [*where, '--whole', 'apt:arches=arm64'])
    assert result.exit_code == 0, result.output
    assert 'apt: arches arm64 sent whole (--whole)' in result.output
    refused = CliRunner().invoke(app, ['--config', str(md.parent), verb, '-n', 'work', '--full',
                                       '--whole', 'apt:arches=arm64'])
    assert refused.exit_code == 1 and 'cannot be combined with --full' in refused.output


def test_tui_sends_whole(wiped):
    from textual.widgets import Input, Select

    from pickers import confirm_picker
    from test_snapshot_tui import _finished, _run
    from waiting import shows, until
    cfg, net, md, _ = wiped

    async def check(app, pilot):
        await pilot.press('t')
        await until(pilot, lambda: type(app.screen).__name__ == 'BundlePickPage' and app.screen.query('#pick-to'))
        app.screen.query_one('#pick-from', Select).value = 'date'
        app.screen.query_one('#pick-since', Input).value = '2026-11-01'
        app.screen.query_one('#pick-whole', Input).value = 'apt:arches=arm64'
        app.screen.query_one('#pick-installer', Select).value = 'no'
        await shows(pilot, lambda: app.screen.query_one('#pick-estimate').render(),
                    'apt: arches arm64 sent whole (--whole)')
        await confirm_picker(app, pilot, 'BundlePickPage')
        await until(pilot, lambda: _finished(app))
        assert app.active_transfer.outcome == 'ok', app.notes

    _run(cfg, md.parent, check)
    [out] = (md.parent / 'usb').glob('*.tar')
    assert _carries(out) == {'arm64', 'new'}
