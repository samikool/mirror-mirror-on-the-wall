"""A VS Code source through the real sync engine, offline: resolve → sync →
verify, then a re-sync that sees a new server commit."""
import asyncio
import concurrent.futures
import hashlib

import pytest

from mmw.core import config, downloader
from mmw.core.pool import Pool
from mmw.core.resolve import resolve_sources
from mmw.core.sync_engine import sync_mirror
from mmw.core.verify import verify_mirror
from mmw.providers.vscode import marketplace as mp
from mmw.providers.vscode import provider as prov
from vscode_fakes import FakeUpstream


@pytest.fixture
def up(monkeypatch):
    fake = FakeUpstream()
    monkeypatch.setattr(prov, "_client", fake.client)
    monkeypatch.setattr(mp, "BACKOFF", 0.0)
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
