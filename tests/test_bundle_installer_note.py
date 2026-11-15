"""The CLI says when the far side is due mmw's installer and --installer
wasn't given — a far side without mmw can't apply the bundle."""
from typer.testing import CliRunner

from mmw import cli
from mmw.core import config

from test_bundle_mmw import _mmw
from test_dry_run_totals import two_mirrors  # noqa: F401


def test_the_cli_says_the_installer_is_due(two_mirrors, tmp_path, monkeypatch):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    d = tmp_path / "cfg"
    d.mkdir()
    config.save(cfg, d)
    monkeypatch.setattr("mmw.core.runtime.installer_due", lambda network, mirror_dir: True)
    r = CliRunner().invoke(cli.app, ["--config", str(d), "bundle", "-n", net.name, "--out", str(tmp_path / "o"),
                                     "--allow-incomplete"])
    assert r.exit_code == 0, r.output
    assert "add --installer" in r.output
