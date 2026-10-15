"""A VS Code source through the real sync engine, offline: resolve → sync →
verify, then a re-sync that sees a new server commit."""
import asyncio
import concurrent.futures
import hashlib
import threading

import pytest

from mmw.core import config, downloader
from mmw.core.pool import Pool
from mmw.core.resolve import resolve_sources
from mmw.core.sync_engine import SyncEvents, classify_mirror, sync_mirror
from mmw.core.verify import verify_mirror
from mmw.providers.vscode import marketplace as mp
from mmw.providers.vscode import provider as prov
from vscode_fakes import FakeUpstream


@pytest.fixture
def up(monkeypatch):
    fake = FakeUpstream()
    monkeypatch.setattr(prov, "_client", fake.client)
    monkeypatch.setattr(mp, "BACKOFF", 0.0)
    monkeypatch.setattr(mp, "RATE_LIMIT_WAIT", 0.0)
    monkeypatch.setattr(downloader, "_thread_client", fake.client)
    return fake


def _src():
    return config.VscodeSource(provider="vscode", kinds=["extensions", "server"],
                               platforms=["linux-x64"], qualities=["stable"],
                               extensions=config.ExtensionSelection(ids=["ms-python.python"]))


def _sync(tmp_path, src):
    units, notes, errors = resolve_sources([src])
    assert not errors, errors
    ex = concurrent.futures.ThreadPoolExecutor(4)

    async def go():
        with Pool(tmp_path) as pool:
            return [await sync_mirror(u, pool=pool, mirror_dir=tmp_path, max_connections=4,
                                      executor=ex) for u in units]
    try:
        return units, asyncio.run(go())
    finally:
        ex.shutdown()


def test_sync_then_verify(up, tmp_path):
    units, summaries = _sync(tmp_path, _src())
    assert all(s.ok for s in summaries), [s.errors + s.metadata_errors for s in summaries]
    with Pool(tmp_path, read_only=True) as pool:
        for u in units:
            r = verify_mirror(u, tmp_path, pool)
            assert r.complete, (u.name, r.describe())


def test_interrupted_extensions_sync_reuses_completed_assets_after_restart(up, tmp_path):
    from mmw.providers import get_provider
    src = config.VscodeSource(provider='vscode', kinds=['extensions'], platforms=['linux-x64'],
                             extensions={'ids': ['ms-python.python']})
    units, _, errors = resolve_sources([src])
    assert not errors
    provider = get_provider('vscode')
    unit = next(u for u in units if provider.fetch_all(u, up.client())[0])
    unit_name = unit.name
    asset_urls = {p.url for p in provider.fetch_all(unit, up.client())[0]}
    stop = threading.Event()
    completed = []
    def halt(name, result):
        if result.status == downloader.Status.DOWNLOADED:
            completed.append(result.checksum)
            stop.set()

    async def interrupted():
        with Pool(tmp_path) as pool, concurrent.futures.ThreadPoolExecutor(1) as executor:
            summary = await sync_mirror(unit, pool=pool, mirror_dir=tmp_path, max_connections=1,
                                        executor=executor, stop_event=stop, events=SyncEvents(progress=halt))
            assert summary.stopped and summary.downloaded == 1
            assert not pool.ref_map(unit.name)
    asyncio.run(interrupted())
    assert len(completed) == 1
    first_urls = {str(r.url) for r in up.requests if r.method == 'GET'} & asset_urls
    assert len(first_urls) == 1
    up.requests.clear()
    # Re-resolve as a new invocation does, and reopen the pool.
    units, _, errors = resolve_sources([src])
    assert not errors
    unit = next(u for u in units if u.name == unit_name)
    async def restarted():
        with Pool(tmp_path) as pool, concurrent.futures.ThreadPoolExecutor(2) as executor:
            summary = await sync_mirror(unit, pool=pool, mirror_dir=tmp_path, max_connections=2, executor=executor)
            assert summary.ok and summary.pooled >= 1
            assert summary.items.completed == summary.items.total
            assert verify_mirror(unit, tmp_path, pool).complete
    asyncio.run(restarted())
    assert first_urls.isdisjoint({str(r.url) for r in up.requests if r.method == 'GET'})


def test_resync_keeps_previous_server_commits(up, tmp_path):
    def build(commit, ts):
        return {"url": f"https://vscode.download.prss.microsoft.com/dbazure/download/stable/"
                       f"{commit}/vscode-server-linux-x64.tar.gz", "name": "1", "version": commit,
                "productVersion": "1", "timestamp": ts,
                "sha256hash": hashlib.sha256(downloader_bytes(commit)).hexdigest()}

    def downloader_bytes(commit):
        from vscode_fakes import fake_asset_bytes
        return fake_asset_bytes(f"https://vscode.download.prss.microsoft.com/dbazure/download/"
                                f"stable/{commit}/vscode-server-linux-x64.tar.gz")

    up.update_overrides["cli-alpine-x64"] = 204
    for i, commit in enumerate(("a" * 40, "b" * 40, "c" * 40, "d" * 40), start=1):
        up.update_overrides["server-linux-x64"] = build(commit, i)
        units, summaries = _sync(tmp_path, _src())
        assert all(s.ok for s in summaries)
    server = next(u for u in units if u.dims["kind"] == "server")
    with Pool(tmp_path, read_only=True) as pool:
        kept = {p.rsplit("/", 1)[1][:-5] for p in pool.ref_map(server.name)
                if p.startswith(".vscode-meta/update/server-linux-x64/")}
    assert kept == {"d" * 40, "c" * 40, "b" * 40}


def test_dry_run_command_reports_vscode_units(up, tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from mmw.cli import app
    config.save(config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"),
                              sources=[_src()]), tmp_path)
    r = CliRunner().invoke(app, ["--config", str(tmp_path), "dry-run"])
    assert r.exit_code == 0, r.output
    assert "vscode-extensions-linux-x64" in r.output and "vscode-server-linux-x64-stable" in r.output


def test_extension_progress_counts_items_and_resync_counts_them_as_pooled(up, tmp_path):
    from mmw.core.item_progress import ItemProgressSession
    src = config.VscodeSource(provider='vscode', kinds=['extensions'], platforms=['linux-x64'],
                             extensions={'ids': ['ms-python.python']})
    units, _, errors = resolve_sources([src])
    assert not errors
    from mmw.providers import get_provider
    provider = get_provider('vscode')
    expected = {u.name: {p.item_id for p in provider.fetch_all(u, up.client())[0]} for u in units}
    unique = set().union(*expected.values())

    async def go():
        with Pool(tmp_path) as pool, concurrent.futures.ThreadPoolExecutor(4) as executor:
            for resync in (False, True):
                session = ItemProgressSession(units)
                for unit in units:
                    inventories, events, files = [], [], []
                    summary = await sync_mirror(unit, pool=pool, mirror_dir=tmp_path, max_connections=4,
                                                executor=executor, items_session=session, events=SyncEvents(
                                                    items_started=inventories.append, item_progress=events.append,
                                                    progress=lambda name, r: files.append((name, r))))
                    assert summary.ok
                    assert inventories[0].ids == expected[unit.name]
                    assert summary.items.total == len(expected[unit.name])
                    assert len(events) == summary.items.total
                    assert len(files) == summary.total
                    if resync:
                        assert summary.items.pooled == summary.items.total
                        assert summary.bytes_transferred == 0
                    else:
                        assert summary.items.downloaded == summary.items.total
                        assert sum(e.result.bytes_transferred for e in events) == summary.bytes_transferred
                    stats = await classify_mirror(unit, pool, executor)
                    assert stats.total == stats.in_pool == len(expected[unit.name])
                    assert stats.to_fetch == 0
                    assert stats.item_label == 'extensions'
                assert session.totals.total == len(unique)
                assert session.totals.completed == len(unique)
    asyncio.run(go())


def test_one_failed_extension_asset_reports_one_failed_item(up, tmp_path, monkeypatch):
    from mmw.providers import get_provider
    src = config.VscodeSource(provider='vscode', kinds=['extensions'], platforms=['linux-x64'],
                             extensions={'ids': ['ms-python.python']})
    units, _, errors = resolve_sources([src])
    assert not errors
    provider = get_provider('vscode')
    unit = next(u for u in units if provider.fetch_all(u, up.client())[0])
    packages = provider.fetch_all(unit, up.client())[0]
    broken = packages[0]
    original = downloader._download_sync
    def download(package, *args, **kwargs):
        if package.repo_path == broken.repo_path:
            raise downloader.PermanentFetchError('asset unavailable')
        return original(package, *args, **kwargs)
    monkeypatch.setattr(downloader, '_download_sync', download)
    async def scenario():
        with Pool(tmp_path) as pool, concurrent.futures.ThreadPoolExecutor(4) as executor:
            events = []
            summary = await sync_mirror(unit, pool=pool, mirror_dir=tmp_path, max_connections=4,
                                        executor=executor, events=SyncEvents(item_progress=events.append))
            assert not summary.ok
            assert summary.items.failed == 1
            failed = [e for e in events if e.result.status == downloader.Status.FAILED]
            assert len(failed) == 1
            assert failed[0].id == broken.item_id
            assert failed[0].result.error == 'asset unavailable'
            assert summary.items.completed == summary.items.total
    asyncio.run(scenario())
