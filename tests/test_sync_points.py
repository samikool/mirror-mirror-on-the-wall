"""After a sync, each network's points: one per repository suite, recorded
only when it changed, shared where two networks see the same files."""
import pytest

from mmw.core import points
from mmw.core.pool import Pool
from mmw.core.snapshots import take_snapshot, walk_tree

from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply_timeline import set_a
from test_snapshot_repo_versions import _change_a, _key


@pytest.fixture
def oak(two_mirrors):  # noqa: F811
    cfg, net, md = two_mirrors
    set_a(md, 1)
    return cfg, net, md


def _idents(md, network):
    with Pool(md, read_only=True) as pool:
        return sorted((t.repo, t.name, len(pool.network_points(network, t.id))) for t in pool.network_tracks(network))


def test_sync_records_a_point_per_suite(oak):
    cfg, net, md = oak
    r = points.record_network_points(net, cfg, md)
    # (no network track: this network serves no files of its own — no keys, no nginx in the walk)
    assert sorted(_idents(md, "oak")) == [(_key(cfg, "a"), "example.com/a", 1), (_key(cfg, "b"), "", 1)]
    assert any("2026-11-01" in n for n in r.new)


def test_an_unchanged_sync_adds_no_point(oak):
    cfg, net, md = oak
    points.record_network_points(net, cfg, md)
    r = points.record_network_points(net, cfg, md)
    assert r.new == [] and all(n == 1 for *_x, n in _idents(md, "oak"))
    set_a(md, 2)
    r = points.record_network_points(net, cfg, md)
    assert [n for repo, _t, n in _idents(md, "oak") if repo == _key(cfg, "a")] == [2] and len(r.new) == 1


def test_releaseless_content_change_is_a_new_point(oak):
    cfg, net, md = oak
    points.record_network_points(net, cfg, md)
    _change_a(md)                                       # a loses its Release: its files become the "" track
    points.record_network_points(net, cfg, md)
    assert (_key(cfg, "a"), "", 1) in _idents(md, "oak")


def test_networks_share_identical_points(oak):
    cfg, net, md = oak
    elm = net.model_copy(update={"name": "elm"})
    cfg2 = cfg.model_copy(update={"networks": [net, elm]})
    points.record_network_points(net, cfg2, md)
    points.record_network_points(elm, cfg2, md)
    with Pool(md, read_only=True) as pool:
        def repo_points(n):
            return {p.id for t in pool.network_tracks(n) if not t.repo.startswith("network:")
                    for p in pool.network_points(n, t.id)}
        oak_ids, elm_ids = repo_points("oak"), repo_points("elm")
    assert oak_ids and oak_ids == elm_ids                      # each network's own files stay its own


def test_after_sync_records_every_network_and_survives_a_failed_walk(oak, monkeypatch):
    cfg, net, md = oak
    elm = net.model_copy(update={"name": "elm"})
    cfg2 = cfg.model_copy(update={"networks": [net, elm]})
    real = points.walk_tree

    def flaky(network, *a, **k):
        if network.name == "elm":
            raise ValueError("snapshot of elm not taken: a key could not be frozen")
        return real(network, *a, **k)
    monkeypatch.setattr(points, "walk_tree", flaky)
    said = []
    reports = points.record_points_after_sync(cfg2, md, {_key(cfg, "a")}, say=said.append)
    by = {r.network: r for r in reports}
    assert by["oak"].new and by["elm"].warnings and not by["elm"].new
    assert any("elm" in s and "could not" in s for s in said)


def test_take_snapshot_unchanged(oak):
    cfg, net, md = oak
    with Pool(md) as pool:
        rows, settings, _w = walk_tree(net, cfg, md, pool)
    snap = take_snapshot(net, cfg, md)
    with Pool(md, read_only=True) as pool:
        assert dict(pool.snapshot_files(snap.id)) == rows
    assert snap.settings == settings


def test_the_cli_sync_records_points_for_what_synced(tmp_path, monkeypatch):
    import asyncio
    from dataclasses import replace

    import httpx

    from mmw.commands import mirror as command
    from mmw.core import config, sync_engine
    from test_download_deduplication import BODY, package, transport
    transport(monkeypatch, lambda request: httpx.Response(200, content=BODY))
    units = [config.MirrorConfig(name="one", provider="apt", source="https://one.test/apt",
                                 source_url="https://one.test/apt")]
    monkeypatch.setattr(command, "resolve_and_report", lambda cfg: (units, []))
    monkeypatch.setattr(sync_engine, "fetch_all_threaded", lambda provider, unit, **kwargs: (
        [replace(package("asset", "https://one.test/asset", known=False), repo_path="one/asset")], []))
    seen = []
    monkeypatch.setattr(points, "record_points_after_sync",
                        lambda cfg, md, synced, say=print, **kwargs: seen.append(synced) or [])
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "mirror"))
    assert asyncio.run(command._mirror_all(cfg)) == 0
    assert seen == [{"https://one.test/apt"}]


def test_ctrl_c_while_recording_points_keeps_the_sync(tmp_path, monkeypatch, capsys):
    """The sync finished; interrupting what follows never turns it into an
    interrupted sync — the points wait for the next one."""
    import asyncio
    from dataclasses import replace

    import httpx

    from mmw.commands import mirror as command
    from mmw.core import config, sync_engine
    from test_download_deduplication import BODY, package, transport
    transport(monkeypatch, lambda request: httpx.Response(200, content=BODY))
    units = [config.MirrorConfig(name="one", provider="apt", source="https://one.test/apt",
                                 source_url="https://one.test/apt")]
    monkeypatch.setattr(command, "resolve_and_report", lambda cfg: (units, []))
    monkeypatch.setattr(sync_engine, "fetch_all_threaded", lambda provider, unit, **kwargs: (
        [replace(package("asset", "https://one.test/asset", known=False), repo_path="one/asset")], []))

    def interrupted(cfg, md, synced, say=print, **kwargs):
        import os
        import signal
        import time
        os.kill(os.getpid(), signal.SIGINT)              # a real Ctrl-C, through asyncio's own handler
        time.sleep(2)
        raise AssertionError("the interrupt never reached the recording")
    monkeypatch.setattr(points, "record_points_after_sync", interrupted)
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "mirror"))
    assert asyncio.run(command._mirror_all(cfg)) == 0
    assert "the next sync records them" in capsys.readouterr().err


def test_recording_points_beside_an_open_busy_pool(oak):
    """The TUI keeps a Pool open and reads from it while a sync records its
    points through another: neither waits out SQLite's busy timeout."""
    import threading
    import time
    cfg, net, md = oak
    stop, errors = threading.Event(), []

    def reader():
        try:
            with Pool(md, read_only=True) as pool:
                while not stop.is_set():
                    pool.network_tracks(net.name)
                    pool.recorded_repos(net.name)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    with Pool(md) as held:                               # the TUI's own, open throughout
        t = threading.Thread(target=reader)
        t.start()
        try:
            started = time.monotonic()
            for day in range(3):
                _change_a(md) if day == 1 else None
                points.record_network_points(net, cfg, md, stamp=f"2026111{day}T000000Z")
                held.network_tracks(net.name)
            assert time.monotonic() - started < 4.5          # never one busy timeout (5 s)
        finally:
            stop.set()
            t.join()
    assert not errors
