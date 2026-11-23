"""Points screens show dates, never raw stamps; warnings say what happened."""
import hashlib

from mmw.core import points
from mmw.core.pool import KIND_META, Pool
from test_transfer_repository_selection import synced  # noqa: F401


def _half_sync(md, units):
    """Upstream moved to 02 Nov; amd64 followed it, arm64's sync failed."""
    from test_repository_points import change_release
    amd = next(u for u in units if u.arch == 'amd64')
    index = b'Package: shared\nFilename: pool/shared.deb\n\n'
    cs = hashlib.sha256(index).hexdigest()
    release = ('Suite: stable\nDate: Mon, 02 Nov 2026 00:00:00 UTC\nSHA256:\n'
               + ''.join(f' {cs} {len(index)} main/binary-{arch}/Packages\n' for arch in ('amd64', 'arm64')))
    with Pool(md) as pool:
        entries = [(c, path, kind) for path, _, _, kind, c in pool.mirror_files([amd.name])
                   if not path.endswith('/Packages')]
        pool.add(index, cs, 'sha256', 'Packages', len(index))
        pool.reconcile_refs(amd.name, [*entries, (cs, 'dists/stable/main/binary-amd64/Packages', KIND_META)],
                            amd.source)
        change_release(pool, amd, release.encode())


def test_partial_sync_warning_names_the_arch_and_the_date_kept(synced):  # noqa: F811
    from mmw.core import config
    cfg, _, md, units = synced
    net = config.NetworkConfig(name='both', nginx=False, certs=False,
                               select=[config.NetworkSelect(source=units[0].source)])
    cfg = cfg.model_copy(update={'networks': [net]})
    _half_sync(md, units)
    report = points.record_network_points(net, cfg, md, after_sync=True)
    said = ' '.join(report.warnings)
    assert "arm64 didn't sync — stable stays at 2026-11-01 until it does" in said, said
    assert 'before bundling' not in said


def test_points_screens_show_no_raw_stamps(synced, tmp_path, monkeypatch):  # noqa: F811
    """Networks, Transfer, Publish and History, after a transfer: dates only."""
    import re
    from mmw.core import snapshots
    from mmw.core.publisher.bundle import bundle_network
    from test_snapshot_tui import _run
    from waiting import until
    cfg, net, md, _ = synced
    monkeypatch.setattr(snapshots, "now_stamp", lambda: "20261123T015441Z")
    bundle_network(net, cfg, md, out_path=tmp_path / "first.tar")
    stamp = re.compile(r"\d{8}T\d{6}Z")
    seen = {}

    async def check(app, pilot):
        await until(pilot, lambda: any("transferred" in str(lbl.render())
                                       for lbl in app.screen.query(".detail-meta")))
        seen["Networks"] = app.export_screenshot()
        for key, page, ready in (("t", "BundlePickPage", "#pick-from"), ("p", "PublishPickPage", "#pick-dir")):
            await pilot.press(key)
            await until(pilot, lambda: type(app.screen).__name__ == page and app.screen.query(ready))
            await pilot.pause()
            seen[page] = app.export_screenshot()
            await pilot.press("escape")
            await until(pilot, lambda: type(app.screen).__name__ == "NetworksPage")
        await pilot.press("h")
        await until(pilot, lambda: type(app.screen).__name__ == "HistoryPage"
                    and app.screen.query_one("#history-table").row_count > 1)
        seen["History"] = app.export_screenshot()
    _run(cfg, tmp_path, check)
    assert len(seen) == 4
    for page, svg in seen.items():
        assert not stamp.search(svg), (page, stamp.search(svg).group())
