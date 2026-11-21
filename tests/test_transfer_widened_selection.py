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
