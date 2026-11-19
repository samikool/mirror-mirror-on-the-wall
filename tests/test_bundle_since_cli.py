"""The CLI exposes date baselines; obsolete snapshot endpoints are gone."""
import pytest
from typer.testing import CliRunner

from mmw import cli
from mmw.core import config
from mmw.core.publisher.bundle import bundle_network

from test_bundle_format3 import mmw_net  # noqa: F401
from test_bundle_since import clock
from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply_timeline import set_a


def invoke(env, tmp_path, *args):
    cfg, net, md = env
    config.save(cfg, md.parent)
    return CliRunner().invoke(cli.app, ["--config", str(md.parent), "bundle", "-n", net.name,
                                       "--out", str(tmp_path / "out"), "--allow-incomplete", *args])


def test_since_cli_names_manifest(mmw_net, tmp_path, monkeypatch):  # noqa: F811
    cfg, net, md = mmw_net
    clock(monkeypatch, 1)
    first = bundle_network(net, cfg, md, out_path=tmp_path / "kept")
    set_a(md, 2)
    clock(monkeypatch, 2)
    r = invoke(mmw_net, tmp_path, "--since", "2026-11-01", "--bundles", str(first.out_path.parent))
    assert r.exit_code == 0, r.output
    assert f"since {first.out_path.name} (its manifest)" in r.output


def test_snapshot_from_option_is_removed(mmw_net, tmp_path):  # noqa: F811
    r = invoke(mmw_net, tmp_path, "--from", "none")
    assert r.exit_code == 2 and "No such option" in r.output


def test_since_nothing_found_is_exit_one(mmw_net, tmp_path):  # noqa: F811
    r = invoke(mmw_net, tmp_path, "--since", "2000-01-01")
    assert r.exit_code == 1
    assert "--full" in r.output


@pytest.mark.parametrize("args", [("--full",)])
def test_since_conflicting_start_is_refused(mmw_net, tmp_path, args):  # noqa: F811
    r = invoke(mmw_net, tmp_path, "--since", "2026-11-01", *args)
    assert r.exit_code == 1
    assert "cannot be combined" in r.output


def test_stage_since_has_the_same_cli_door(mmw_net, tmp_path, monkeypatch):  # noqa: F811
    cfg, net, md = mmw_net
    clock(monkeypatch, 1)
    first = bundle_network(net, cfg, md, out_path=tmp_path / "kept")
    set_a(md, 2)
    clock(monkeypatch, 2)
    config.save(cfg, md.parent)
    r = CliRunner().invoke(cli.app, ["--config", str(md.parent), "stage", "-n", net.name,
                                    "--out", str(tmp_path / "stage"), "--allow-incomplete",
                                    "--since", "2026-11-01", "--bundles", str(first.out_path.parent)])
    assert r.exit_code == 0, r.output
    assert f"since {first.out_path.name} (its manifest)" in r.output
