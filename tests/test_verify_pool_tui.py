"""Verify pool: `mmw verify --stat --repair` and the TUI's Verify pool page are
one command (`commands.verify.verify_pool` / `repair_pool`), tested through each."""
import asyncio
import gzip
import hashlib

import pytest
from typer.testing import CliRunner

from mmw.cli import app
from mmw.core import config
from mmw.core.pool import KIND_META, KIND_PKG, Pool

KEY = "https://ex.org/debian"


@pytest.fixture
def gutted(tmp_path):
    """A synced-looking pool whose one package blob is gone from disk — what
    a pool directory moved without its files leaves."""
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "mirror"),
                        sources=[config.SourceConfig(url=KEY, suites=["jammy"],
                                                     components=["main"], arches=["amd64"])])
    config.save(cfg, tmp_path)
    [unit] = cfg.sources[0].to_mirrors()
    deb = b"package-bytes"
    deb_cs = hashlib.sha256(deb).hexdigest()
    index = gzip.compress(f"Package: a\nFilename: pool/a.deb\nSize: {len(deb)}\nSHA256: {deb_cs}\n\n".encode())
    index_cs = hashlib.sha256(index).hexdigest()
    with Pool(cfg.settings.mirror_dir) as pool:
        pool.add(deb, deb_cs, "sha256", "a.deb", len(deb))
        pool.add(index, index_cs, "sha256", "Packages.gz", len(index))
        pool.reconcile_refs(unit.name, [(deb_cs, "pool/a.deb", KIND_PKG),
                                        (index_cs, "dists/jammy/main/binary-amd64/Packages.gz", KIND_META)])
        blob = pool.blob_file(deb_cs)
    blob.unlink()
    return cfg, tmp_path, deb_cs


def _pooled(cfg, checksum):
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        return bool(pool.batch_contains([checksum]))


def test_cli_verify_finds_and_repairs(gutted):
    cfg, folder, cs = gutted
    checked = CliRunner().invoke(app, ["--config", str(folder), "verify", "--stat"])
    assert checked.exit_code == 1 and "1 damaged blob(s)" in checked.output, checked.output
    assert _pooled(cfg, cs)                                          # a check never writes
    repaired = CliRunner().invoke(app, ["--config", str(folder), "verify", "--repair"])
    assert "Dropped 1 damaged blob(s)" in repaired.output, repaired.output
    assert not _pooled(cfg, cs)                                      # the next sync fetches it


def test_tui_verify_pool_finds_and_repairs(gutted, monkeypatch):
    from mmw.tui.app import MmwApp
    from mmw.tui.ui.dialog import ConfirmDialog
    from mmw.tui.ui.widgets import Button
    from waiting import shows, until
    cfg, folder, cs = gutted
    synced = []

    async def fake_sync(self, selection, **_k):
        synced.append(selection)
    monkeypatch.setattr(MmwApp, "start_sync", fake_sync)

    async def go():
        tui = MmwApp(cfg, folder, initial_section="repositories")
        async with tui.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: tui.screen.query("#repo-list"))
            assert "verify_pool" in tui.screen.MORE
            tui.screen.query_one("#repo-list").focus()
            await pilot.press("V")
            await until(pilot, lambda: type(tui.screen).__name__ == "VerifyPoolPage")
            page = tui.screen
            await until(pilot, lambda: page.query("#btn-verify-stat"))
            assert page.query_one("#btn-verify-repair", Button).disabled
            page.query_one("#btn-verify-stat", Button).press()
            await shows(pilot, lambda: page.query_one("#verify-result").render(), "1 damaged blob(s)")
            assert _pooled(cfg, cs)
            await until(pilot, lambda: not page.query_one("#btn-verify-repair", Button).disabled)
            page.query_one("#btn-verify-repair", Button).press()
            await until(pilot, lambda: isinstance(tui.screen, ConfirmDialog))
            tui.screen.query_one("#confirm", Button).press()
            await shows(pilot, lambda: page.query_one("#page-status").render(), "Dropped 1 damaged blob(s)")
            assert not _pooled(cfg, cs)
            await until(pilot, lambda: not page.query_one("#btn-verify-sync", Button).disabled)
            page.query_one("#btn-verify-sync", Button).press()
            await until(pilot, lambda: synced == [([], [])])
    asyncio.run(go())

