"""What changed between two points (`core/point_diff`), and `mmw history diff`."""
import gzip
import hashlib
import lzma
import time
import tracemalloc

import pytest
from typer.testing import CliRunner

from mmw.cli import app
from mmw.commands import history
from mmw.core import point_diff as pd
from mmw.core import points
from mmw.core.pool import Pool
from test_transfer_repository_selection import synced  # noqa: F401


def _index(packages: dict) -> bytes:
    """A Packages text: name → version, or (name, arch) → version."""
    out = []
    for key, version in packages.items():
        name, arch = key if isinstance(key, tuple) else (key, 'amd64')
        out.append(f'Package: {name}\nVersion: {version}\nArchitecture: {arch}\nFilename: pool/{name}.deb\n\n')
    return ''.join(out).encode()


def _point(packages: dict, ext: str = '.xz', arch: str = 'amd64'):
    data = {'.xz': lzma.compress, '.gz': gzip.compress, '': lambda b: b}[ext](_index(packages))
    path = f'ex.org/debian/dists/jammy/main/binary-{arch}/Packages{ext}'
    cs = hashlib.sha256(data).hexdigest()
    return {path: cs}, {path: data}


def _of(packages, ext='.xz'):
    files, blobs = _point(packages, ext)
    return pd.packages_of(files, blobs.get)


def test_diff_names_upgrades_adds_and_removals():
    a = _of({'alpha': '1.0', 'beta': '2.0', 'delta': '0.5', 'same': '1'})
    b = _of({'alpha': '1.0+deb1', 'beta': '2.0', 'gamma': '3.0', 'same': '1'}, ext='.gz')
    d = pd.diff(a, b)
    assert d.upgraded == [('alpha', 'amd64', '1.0', '1.0+deb1')]
    assert d.added == [('gamma', 'amd64', None, '3.0')]
    assert d.removed == [('delta', 'amd64', '0.5', None)]
    assert d.downgraded == []
    assert pd.summary(d) == '+1 ~1 −1'
    assert pd.brings(d) == 'brings 1 upgrade, 1 new package, 1 removal'
    assert pd.lines(d) == ['~ alpha 1.0 → 1.0+deb1 (amd64)', '+ gamma 3.0 (amd64)', '− delta 0.5 (amd64)']
    assert pd.brings(pd.diff(a, a)) == 'no package changes' and pd.summary(pd.diff(a, a)) == '+0 ~0 −0'


def test_downgrade_is_its_own_kind():
    # Debian ordering, not text: 1.10 > 1.9, ~ sorts before anything, epochs win.
    a = _of({'alpha': '1.10', 'beta': '1:0.1', 'tilde': '2.0'})
    b = _of({'alpha': '1.9', 'beta': '2.0', 'tilde': '2.0~rc1'})
    d = pd.diff(a, b)
    assert {c[0] for c in d.downgraded} == {'alpha', 'beta', 'tilde'} and not d.upgraded
    assert pd.summary(d) == '+0 ~0 −0 ↓3'
    assert pd.brings(d) == 'brings 3 downgrades'
    assert pd.lines(d)[0] == '↓ alpha 1.10 → 1.9 (amd64)'


def test_each_arch_is_its_own_package_and_the_highest_version_counts():
    files_amd, blobs_amd = _point({('alpha', 'amd64'): '1', ('doc', 'all'): '1'})
    files_arm, blobs_arm = _point({('alpha', 'arm64'): '2', ('doc', 'all'): '1'},
                                  arch='arm64')
    files = {**files_amd, **files_arm}
    blobs = {**blobs_amd, **blobs_arm}
    got = pd.packages_of(files, blobs.get)
    assert got == {('alpha', 'amd64'): '1', ('alpha', 'arm64'): '2', ('doc', 'all'): '1'}
    text = b'Package: x\nVersion: 1.0\n\nPackage: x\nVersion: 1.2\n\nPackage: x\nVersion: 1.1\n\n'
    path = 'r/dists/s/main/binary-i386/Packages'
    assert pd.packages_of({path: 'cs-x'}, {path: text}.get) == {('x', 'i386'): '1.2'}   # arch from the path


def test_prefers_a_compressed_variant_and_skips_by_hash():
    base = 'ex.org/debian/dists/jammy/main/binary-amd64/'
    reads = []
    files = {base + 'Packages': 'plain', base + 'Packages.xz': 'xz', base + 'by-hash/SHA256/abc': 'abc'}
    blobs = {base + 'Packages': _index({'alpha': '1'}), base + 'Packages.xz': lzma.compress(_index({'alpha': '1'}))}

    def read(path):
        reads.append(path)
        return blobs.get(path)
    pd.clear_cache()
    assert pd.packages_of(files, read) == {('alpha', 'amd64'): '1'}
    assert reads == [base + 'Packages.xz']
    pd.packages_of(files, read)                    # cached by the index's checksum
    assert reads == [base + 'Packages.xz']


def test_large_index_streams():
    text = ''.join(f'Package: pkg{i}\nVersion: 1.{i % 7}-{i % 3}ubuntu1\nArchitecture: amd64\n'
                   f'Depends: libc6 (>= 2.34), libfoo{i % 50}\nFilename: pool/main/p/pkg{i}/pkg{i}_amd64.deb\n'
                   f'Size: {i * 3}\nSHA256: {i:064x}\nDescription: package number {i}\n'
                   f' a long description line for it\n\n' for i in range(100_000)).encode()
    data = lzma.compress(text, preset=1)
    del text
    path = 'ex.org/debian/dists/jammy/main/binary-amd64/Packages.xz'
    pd.clear_cache()
    started = time.monotonic()
    got = pd.packages_of({path: 'big'}, {path: data}.get)
    assert time.monotonic() - started < 5
    assert len(got) == 100_000 and got[('pkg8', 'amd64')] == '1.1-2ubuntu1'
    pd.clear_cache()
    tracemalloc.start()
    try:
        got = pd.packages_of({path: 'big'}, {path: data}.get)
        held, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    # The ~33 MB decompressed text is never held: the compressed index, the
    # decompressor's buffers and the result.
    assert peak < 3 * len(data) + held
    pd.clear_cache()


# ── mmw history diff ─────────────────────────────────────────────────────────

def _versioned(cfg, md, unit, day, packages: dict):
    """Sync day `day` of `unit`: its Packages names `packages` (name → version)."""
    from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE
    from test_repository_points import change_release
    with Pool(md) as pool:
        entries = [(cs, path, kind) for path, _, _, kind, cs in pool.mirror_files([unit.name])
                   if not path.endswith('/Packages') and kind != KIND_PKG]
        for name, version in packages.items():          # what the index names is there: a consistent point
            body = f'{name} {version}'.encode()
            cs = hashlib.sha256(body).hexdigest()
            pool.add(body, cs, 'sha256', f'{name}.deb', len(body))
            entries.append((cs, f'pool/{name}.deb', KIND_PKG))
        index = _index(packages)
        cs = hashlib.sha256(index).hexdigest()
        pool.add(index, cs, 'sha256', 'Packages', len(index))
        entries.append((cs, f'dists/stable/main/binary-{unit.arch}/Packages', KIND_META))
        assert any(kind == KIND_RELEASE for _, _, kind in entries)
        pool.reconcile_refs(unit.name, entries, unit.source)
        change_release(pool, unit, f'Suite: stable\nDate: Mon, {day:02} Nov 2026 00:00:00 UTC\n'.encode())
    points.record_points_after_sync(cfg.model_copy(update={'networks': []}), md, {unit.source}, say=lambda _: None)


@pytest.fixture
def versions(synced, tmp_path):  # noqa: F811
    """02 Nov: alpha 1.0, beta 2.0, delta 0.5; 03 Nov: alpha 1.1, gamma 1.0 (delta gone);
    04 Nov: alpha 1.2."""
    cfg, net, md, units = synced
    unit = next(u for u in units if u.arch == 'amd64')
    _versioned(cfg, md, unit, 2, {'alpha': '1.0', 'beta': '2.0', 'delta': '0.5'})
    _versioned(cfg, md, unit, 3, {'alpha': '1.1', 'beta': '2.0', 'gamma': '1.0'})
    _versioned(cfg, md, unit, 4, {'alpha': '1.2', 'beta': '2.0', 'gamma': '1.0'})
    return cfg, net, md, unit, tmp_path


def test_history_diff_to_the_next_point(versions):
    cfg, net, md, unit, tmp_path = versions
    [d] = history.diff_plan(cfg, unit.source, '2026-11-02')
    assert (d.suite, d.old[:8], d.new[:8]) == ('stable', '20261102', '20261103')
    assert d.changes.upgraded == [('alpha', 'amd64', '1.0', '1.1')]
    assert d.changes.added == [('gamma', 'amd64', None, '1.0')]
    assert d.changes.removed == [('delta', 'amd64', '0.5', None)]


def test_cli_history_diff(versions):
    cfg, net, md, unit, tmp_path = versions
    result = CliRunner().invoke(app, ['--config', str(tmp_path), 'history', 'diff', unit.source,
                                      '2026-11-02', '2026-11-04', '--suite', 'stable'])
    assert result.exit_code == 0, result.output
    out = result.output
    assert 'stable: 2026-11-02 00:00:00 UTC → 2026-11-04 00:00:00 UTC · +1 ~1 −1' in out
    assert '~ alpha 1.0 → 1.2 (amd64)' in out and '+ gamma 1.0 (amd64)' in out and '− delta 0.5 (amd64)' in out
    newest = CliRunner().invoke(app, ['--config', str(tmp_path), 'history', 'diff', unit.source, '2026-11-04'])
    assert newest.exit_code == 1 and 'is the newest kept point of stable' in newest.output
    missing = CliRunner().invoke(app, ['--config', str(tmp_path), 'history', 'diff', unit.source, '2026-10-09'])
    assert missing.exit_code == 1 and 'no kept point' in missing.output


def test_tui_changes_of_a_history_row(versions):
    from test_snapshot_tui import _run
    from test_tui_history_table import _open, _to_day
    from waiting import shows, until
    cfg, net, md, unit, tmp_path = versions

    async def check(app, pilot):
        await _open(app, pilot)
        page = app.screen
        assert 'changes_row' in {b.binding.action for b in page.active_bindings.values() if b.binding.show}
        await _to_day(pilot, page, '03')
        await pilot.press('c')                            # what 03 Nov changed since 02 Nov
        await until(pilot, lambda: type(app.screen).__name__ == 'ChangesPage' and app.screen.is_mounted)
        await shows(pilot, lambda: app.screen.query_one('#changes-text').render(), '~ alpha 1.0 → 1.1 (amd64)')
        text = str(app.screen.query_one('#changes-text').render())
        assert 'stable: 2026-11-02 00:00:00 UTC → 2026-11-03 00:00:00 UTC · +1 ~1 −1' in text
        assert '− delta 0.5 (amd64)' in text
        await pilot.press('escape')
        await until(pilot, lambda: type(app.screen).__name__ == 'HistoryPage')
        oldest = max(i for i, r in enumerate(page._rows) if r is not None)
        page.query_one('#history-table').move_cursor(row=oldest)
        await pilot.pause()
        await pilot.click('#btn-history-changes')
        await shows(pilot, lambda: page.query_one('#page-status').render(), 'nothing before it')
    _run(cfg, tmp_path, check)


# ── what a transfer brings: the preview and the far side's apply review ──────

@pytest.fixture
def sent_then_newer(synced, tmp_path):  # noqa: F811
    """`work` was sent 02 Nov (alpha 1.0, beta 2.0, delta 0.5); 04 Nov has
    alpha 1.2, beta 2.0, gamma 1.0."""
    from mmw.commands.bundle import bundle_network
    from mmw.site import home as H
    from mmw.site.apply import apply_bundle
    cfg, net, md, units = synced
    unit = next(u for u in units if u.arch == 'amd64')
    _versioned(cfg, md, unit, 2, DAY_2 := {'alpha': '1.0', 'beta': '2.0', 'delta': '0.5'})
    first = bundle_network(net, cfg, md, out_path=tmp_path / 'first.tar', full=True).out_path
    home = H.init_root(tmp_path / 'far', name='main', set_id='work', network=net.name)
    apply_bundle(home, str(first), say=lambda *_: None)
    _versioned(cfg, md, unit, 3, {**DAY_2, 'alpha': '1.1'})
    _versioned(cfg, md, unit, 4, {'alpha': '1.2', 'beta': '2.0', 'gamma': '1.0'})
    return cfg, net, md, unit, tmp_path, home


BRINGS = 'brings 1 upgrade, 1 new package, 1 removal'


def test_transfer_preview_says_upgrades(sent_then_newer):
    from mmw.core.transfer_ranges import preview_transfer
    cfg, net, md, unit, tmp_path, home = sent_then_newer
    [row] = preview_transfer(net, md, cfg=cfg).rows
    assert row.from_date[:8] == '20261102' and row.to_date[:8] == '20261104'
    assert row.changes == BRINGS
    result = CliRunner().invoke(app, ['--config', str(tmp_path), 'bundle', '-n', net.name, '--dry-run'])
    assert result.exit_code == 0, result.output
    assert BRINGS in next(x for x in result.output.splitlines() if '→' in x)
    [full] = preview_transfer(net, md, cfg=cfg, full=True).rows
    assert full.changes == ''                       # nothing to compare a full transfer with


def test_site_apply_review_says_upgrades(sent_then_newer):
    from mmw.commands.bundle import bundle_network
    from mmw.site.apply import apply_bundle, plan_apply
    cfg, net, md, unit, tmp_path, home = sent_then_newer
    second = bundle_network(net, cfg, md, out_path=tmp_path / 'second.tar', history=True).out_path
    [row] = plan_apply(home, str(second)).rows
    assert row.changes == (f'stable: {BRINGS}',)
    from mmw.tui.site.apply_flow import _row_text
    assert f'stable: {BRINGS}' in _row_text(row, str(tmp_path / 'far'))
    dry = apply_bundle(home, str(second), dry_run=True, say=lambda *_: None)
    assert any(f'stable: {BRINGS}' in line for line in dry.lines)
