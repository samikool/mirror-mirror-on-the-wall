"""Recover incomplete saved VS Code units without a marketplace rescan."""
import asyncio
import concurrent.futures
import hashlib
import threading

import httpx
import pytest
from typer.testing import CliRunner

from mmw.cli import app
from mmw.core import config, downloader
from mmw.core.pool import KIND_META, KIND_PKG, Pool
from mmw.core.resolve import resolve_sources
from mmw.core.sync_engine import retry_failed_mirror, sync_mirror
from mmw.core.verify import verify_mirror
from mmw.providers import get_provider
from mmw.providers.vscode import marketplace as mp
from mmw.providers.vscode import provider as prov
from vscode_fakes import FakeUpstream, fake_asset_bytes


@pytest.fixture(params=["extensions", "server"])
def incomplete(request, tmp_path, monkeypatch):
    upstream = FakeUpstream()
    blocked = set()

    def handle(req):
        if str(req.url) in blocked:
            upstream.requests.append(req)
            return httpx.Response(404)
        return upstream(req)

    def client():
        return httpx.Client(transport=httpx.MockTransport(handle), follow_redirects=True)

    monkeypatch.setattr(prov, "_client", client)
    monkeypatch.setattr(downloader, "_thread_client", client)
    monkeypatch.setattr(mp, "BACKOFF", 0)
    src = config.VscodeSource(provider="vscode", kinds=[request.param],
                              platforms=["linux-x64"], qualities=["stable"],
                              extensions={"ids": ["ms-python.python"]})
    units, _, errors = resolve_sources([src])
    assert not errors
    provider = get_provider("vscode")
    unit = next(u for u in units if provider.fetch_all(u, client())[0])
    packages, _ = provider.fetch_all(unit, client())
    missing = packages[0]
    blocked.add(missing.url)
    with Pool(tmp_path) as pool, concurrent.futures.ThreadPoolExecutor(2) as ex:
        summary = asyncio.run(sync_mirror(unit, pool=pool, mirror_dir=tmp_path,
                                         max_connections=2, executor=ex))
        assert summary.failed == 1 and summary.metadata_ok
        assert not verify_mirror(unit, tmp_path, pool).complete
    upstream.requests.clear()
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path), sources=[src],
                        networks=[config.NetworkConfig(name="lab", root=tmp_path / "published")])
    return cfg, unit, missing, blocked, upstream


def repair(pool, unit, **kwargs):
    with concurrent.futures.ThreadPoolExecutor(2) as ex:
        return asyncio.run(retry_failed_mirror(unit, pool=pool, max_connections=2,
                                               executor=ex, **kwargs))


def test_repair_only_missing_assets_and_keep_all_existing_refs(incomplete, tmp_path):
    _, unit, missing, blocked, upstream = incomplete
    blocked.clear()
    with Pool(tmp_path) as pool:
        before = pool.ref_map(unit.name)
        result = repair(pool, unit)
        assert result.ok and result.total == result.downloaded == 1
        assert pool.ref_map(unit.name).items() >= before.items()
        assert verify_mirror(unit, tmp_path, pool).complete
        assert pool.resolve(unit.name, missing.repo_path).read_bytes() == fake_asset_bytes(missing.url)
        assert [str(r.url) for r in upstream.requests] == [missing.url]
        upstream.requests.clear()
        assert repair(pool, unit).total == 0
        assert not upstream.requests


def test_failed_retry_retains_snapshot_and_can_be_repeated(incomplete, tmp_path):
    _, unit, _, blocked, upstream = incomplete
    with Pool(tmp_path) as pool:
        before = pool.ref_map(unit.name)
        result = repair(pool, unit)
        assert result.failed == 1 and not result.ok
        assert pool.ref_map(unit.name) == before
        assert len(upstream.requests) == 1  # permanent 404 has no extra attempts
        blocked.clear()
        assert repair(pool, unit).ok
        assert verify_mirror(unit, tmp_path, pool).complete


def test_repair_reuses_asset_downloaded_by_another_unit(incomplete, tmp_path):
    _, unit, missing, _, upstream = incomplete
    with Pool(tmp_path) as pool:
        data = fake_asset_bytes(missing.url)
        pool.add(data, hashlib.sha256(data).hexdigest(), "sha256", missing.filename,
                 len(data), source_url=missing.url)
        result = repair(pool, unit)
        assert result.ok and result.pooled == 1 and result.downloaded == 0
        assert not upstream.requests
        assert verify_mirror(unit, tmp_path, pool).complete


def test_repair_rejects_concurrently_changed_metadata(incomplete, tmp_path, monkeypatch):
    _, unit, _, blocked, _ = incomplete
    blocked.clear()
    with Pool(tmp_path) as pool:
        real = pool.repair_refs
        new_cs = hashlib.sha256(b"new witness").hexdigest()
        path = next(p for p in pool.ref_map(unit.name) if p.startswith(".vscode-meta/"))

        def changed(name, entries, source, witness, *recovered):
            pool.reconcile_refs(name, [(new_cs, path, KIND_META)], source)
            return real(name, entries, source, witness, *recovered)

        monkeypatch.setattr(pool, "repair_refs", changed)
        with pytest.raises(RuntimeError, match="metadata changed"):
            repair(pool, unit)
        assert pool.ref_map(unit.name) == {path: new_cs}


def test_stopped_repair_does_not_commit(incomplete, tmp_path):
    _, unit, _, _, upstream = incomplete
    stop = threading.Event()
    stop.set()
    with Pool(tmp_path) as pool:
        before = pool.ref_map(unit.name)
        result = repair(pool, unit, stop_event=stop)
        assert result.stopped
        assert pool.ref_map(unit.name) == before
        assert not upstream.requests


def test_cli_dry_run_and_real_retry(incomplete, tmp_path):
    cfg, unit, _, blocked, upstream = incomplete
    config.save(cfg, tmp_path)
    runner = CliRunner()
    args = ["--config", str(tmp_path), "retry-failed", "--network", "lab"]
    result = runner.invoke(app, [*args, "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "1 missing file(s)" in result.output
    assert not upstream.requests
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    blocked.clear()
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    with Pool(tmp_path, read_only=True) as pool:
        assert verify_mirror(unit, tmp_path, pool).complete
    assert runner.invoke(app, [*args[:-1], "absent"]).exit_code == 1


def test_unreadable_saved_metadata_aborts_before_any_network(incomplete, tmp_path):
    _, unit, _, _, upstream = incomplete
    with Pool(tmp_path) as pool:
        pool._conn.execute("DELETE FROM retry_state WHERE mirror_name = ?", (unit.name,))
        pool._conn.commit()  # simulate a mirror from before the journal existed
        path = next(p for p in pool.ref_map(unit.name) if p.startswith(".vscode-meta/"))
        pool.resolve(unit.name, path).write_bytes(b"not json")
        with pytest.raises(RuntimeError, match="cannot repair from"):
            repair(pool, unit)
        assert not upstream.requests


def test_repair_without_saved_metadata_refuses(tmp_path):
    unit = get_provider("vscode").expand(config.VscodeSource(
        provider="vscode", kinds=["server"], platforms=["linux-x64"]))[0]
    with Pool(tmp_path) as pool:
        with pytest.raises(RuntimeError, match="normal sync first"):
            repair(pool, unit)


def test_repair_cannot_add_metadata_and_rolls_back(tmp_path):
    with Pool(tmp_path) as pool:
        cs = "a" * 64
        with pytest.raises(ValueError, match="only add package"):
            pool.repair_refs("m", [(cs, "bad", KIND_PKG), (cs, "index", KIND_META)], "s", {})
        assert pool.ref_map("m") == {}


def test_retry_respects_server_backoff(incomplete, tmp_path, monkeypatch):
    from mmw.core.http import RetryTransport

    _, unit, missing, _, _ = incomplete
    events = []

    class Cooldowns:
        def wait(self, host):
            events.append(("wait", host))

        def defer(self, host, seconds):
            events.append(("defer", seconds))

    calls = []

    def respond(req):
        calls.append(str(req.url))
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, content=fake_asset_bytes(missing.url))

    monkeypatch.setattr(downloader, "_thread_client", lambda: httpx.Client(
        transport=RetryTransport(httpx.MockTransport(respond), cooldowns=Cooldowns())))
    with Pool(tmp_path) as pool:
        assert repair(pool, unit).ok
    assert calls == [missing.url, missing.url]
    assert events == [("wait", httpx.URL(missing.url).host), ("defer", 7),
                      ("wait", httpx.URL(missing.url).host)]


def test_tui_retry_button_repairs_without_discovery(incomplete, tmp_path):
    from mmw.tui.app import MmwApp
    from mmw.tui.ui.widgets import Button
    from waiting import settle, until

    cfg, unit, missing, blocked, upstream = incomplete
    blocked.clear()
    config.save(cfg, tmp_path)

    async def scenario():
        app = MmwApp(cfg, tmp_path, initial_section="repositories")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.screen.query_one("#btn-repo-retry", Button).press()
            await until(pilot, lambda: app.active_sync is not None)
            await until(pilot, lambda: not app.active_sync.running and not app.active_sync.draining)
            assert app.current_mode == "sync"
            assert app.active_sync._retry_failed
            assert app.active_sync.item_failures == 0
            assert all(not c.has_class("error") for c in app.active_sync._cards.values()), [
                c._log_history for c in app.active_sync._cards.values()]
    asyncio.run(scenario())
    assert [str(r.url) for r in upstream.requests] == [missing.url]
    with Pool(tmp_path, read_only=True) as pool:
        assert verify_mirror(unit, tmp_path, pool).complete
