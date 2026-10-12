"""`mmw snapshot take|list|rename|delete|clean`."""
from typer.testing import CliRunner

from mmw import cli
from mmw.core import config
from mmw.core.pool import Pool

from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture


def _mmw(monkeypatch, cfg_dir, *args):
    monkeypatch.setattr(cli, "_CFG_DIR", cli._CFG_DIR)   # --config sets it; restored after
    return CliRunner().invoke(cli.app, ["--config", str(cfg_dir), "snapshot", *args])


def test_take_list_rename_delete(two_mirrors, monkeypatch):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    cfg_dir = mirror_dir.parent
    config.save(cfg, cfg_dir)

    r = _mmw(monkeypatch, cfg_dir, "take", "-n", "oak", "--label", "golden")
    assert r.exit_code == 0, r.output
    assert "golden" in r.output and "files" in r.output
    r = _mmw(monkeypatch, cfg_dir, "list", "-n", "oak")
    assert r.exit_code == 0 and "golden" in r.output and "manual" in r.output

    r = _mmw(monkeypatch, cfg_dir, "rename", "-n", "oak", "golden", "pre-upgrade")
    assert r.exit_code == 0, r.output
    r = _mmw(monkeypatch, cfg_dir, "delete", "-n", "oak", "pre-upgrade")
    assert r.exit_code == 0, r.output
    with Pool(mirror_dir, read_only=True) as pool:
        assert pool.snapshots("oak") == []

    r = _mmw(monkeypatch, cfg_dir, "delete", "-n", "oak", "nope")
    assert r.exit_code == 1 and "no snapshot" in r.output


def test_clean_previews_without_yes_and_deletes_with_it(two_mirrors, monkeypatch):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    cfg_dir = mirror_dir.parent
    config.save(cfg, cfg_dir)
    for _ in range(3):
        assert _mmw(monkeypatch, cfg_dir, "take", "-n", "oak").exit_code == 0

    r = _mmw(monkeypatch, cfg_dir, "clean", "-n", "oak", "--keep", "1")
    assert r.exit_code == 0 and "would delete 2" in r.output, r.output
    with Pool(mirror_dir, read_only=True) as pool:
        assert len(pool.snapshots("oak")) == 3

    r = _mmw(monkeypatch, cfg_dir, "clean", "-n", "oak", "--keep", "1", "--yes")
    assert r.exit_code == 0 and "deleted 2" in r.output, r.output
    with Pool(mirror_dir, read_only=True) as pool:
        assert len(pool.snapshots("oak")) == 1


def test_an_unknown_network_is_an_error(two_mirrors, monkeypatch):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    config.save(cfg, mirror_dir.parent)
    r = _mmw(monkeypatch, mirror_dir.parent, "take", "-n", "nope")
    assert r.exit_code == 1 and "nope" in r.output


def test_a_removed_or_renamed_network_takes_its_snapshots_along(tmp_path):
    """Snapshots are GC roots filed under the network's name. A rename left
    them under the old name and a removal left them behind — unreachable by
    every snapshot command, pinning their files in the pool forever, and
    handing their bundle history to any later network of the same name."""
    from mmw.core import config as config_mod
    from mmw.core.pool import Pool
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


def test_snapshots_of_a_network_edited_out_of_the_config_can_be_cleaned(tmp_path, capsys):
    from mmw.commands import snapshot as snap_cmd
    from mmw.core.pool import Pool

    mirror_dir = tmp_path / "mirror"
    (tmp_path / "mmw.toml").write_text(f'[settings]\nmirror_dir = "{mirror_dir}"\n')
    with Pool(mirror_dir) as pool:
        pool.create_snapshot("gone", "20260101T000000Z", kind="bundle", rows=[])
        pool.record_bundle("gone", None, "20260101T000000Z", "/x.tar", 0, 0, 0)

    assert snap_cmd.list_(tmp_path, "gone") == 0
    assert snap_cmd.clean(tmp_path, "gone", keep=0, yes=True) == 0
    assert "Deleted all 1 snapshot(s) of gone" in capsys.readouterr().out
    with Pool(mirror_dir, read_only=True) as pool:
        assert pool.network_names() == set()
    assert snap_cmd.list_(tmp_path, "gone") == 1                     # now truly unknown
