"""`mmw snapshot take|list|rename|delete|clean`."""
from typer.testing import CliRunner

from mmw import cli
from mmw.core.pool import Pool

from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture


def _mmw(monkeypatch, cfg_dir, *args):
    monkeypatch.setattr(cli, "_CFG_DIR", cli._CFG_DIR)   # --config sets it; restored after
    return CliRunner().invoke(cli.app, ["--config", str(cfg_dir), "snapshot", *args])








def test_a_removed_or_renamed_network_takes_its_snapshots_along(tmp_path):
    """Snapshots are GC roots filed under the network's name. A rename left
    them under the old name and a removal left them behind — unreachable by
    every snapshot command, pinning their files in the pool forever, and
    handing their bundle history to any later network of the same name."""
    from mmw.core import config as config_mod
    from mmw.tui import networks

    with Pool(tmp_path) as pool:
        pool.create_snapshot("lab", "20260101T000000Z", kind="manual", rows=[])
        pool.record_bundle("lab", None, "20260101T000000Z", "/x.tar", 0, 0, 0)
        pool.create_snapshot("oak", "20260101T000000Z", kind="manual", rows=[])

    assert config_mod.rename_network_state(tmp_path, "lab", "site") == []
    assert "already in the pool" in config_mod.rename_network_state(tmp_path, "site", "oak")[0]
    with Pool(tmp_path, read_only=True) as pool:
        assert [s.network for s in pool.snapshots("site")] == ["site"]
        assert pool.bundles("site")[0].to_stamp == "20260101T000000Z"
        assert pool.snapshots("lab") == []

    assert networks.clear_ledger(tmp_path, "site") is None
    with Pool(tmp_path, read_only=True) as pool:
        assert pool.snapshots("site") == [] and pool.bundles("site") == []
        assert pool.network_names() == {"oak"}
