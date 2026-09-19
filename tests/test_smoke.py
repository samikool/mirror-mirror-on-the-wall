"""Smoke tests: every module imports, every CLI command parses --help."""
import importlib
import pkgutil

import pytest
from typer.testing import CliRunner

import mmw


def _all_modules() -> list[str]:
    return [m.name for m in pkgutil.walk_packages(mmw.__path__, "mmw.")]


@pytest.mark.parametrize("mod_name", _all_modules())
def test_module_imports(mod_name):
    importlib.import_module(mod_name)


runner = CliRunner()


def test_top_level_help():
    from mmw.cli import app
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0


def _all_commands() -> list[str]:
    """Every command actually registered on the app.

    Derived, never hardcoded. The literal list this replaced had already
    drifted — it never gained `add`, so that command's --help went unsmoked
    from the day it landed, and every command added since would have been
    missed the same silent way. A list that cannot drift is the only kind
    worth having here, since the failure mode is absence of a test rather
    than a failing one.
    """
    from mmw.cli import app
    return sorted(
        c.name or c.callback.__name__.removeprefix("cmd_").replace("_", "-")
        for c in app.registered_commands
    )


@pytest.mark.parametrize("cmd", _all_commands())
def test_command_help(cmd):
    from mmw.cli import app
    result = runner.invoke(app, [cmd, "--help"])
    assert result.exit_code == 0


def test_every_command_is_smoke_tested():
    """Guards the guard: _all_commands() must find them all, so a future
    command cannot slip past by being registered in some other way."""
    assert set(_all_commands()) >= {
        "init", "manage", "sync", "dry-run", "publish", "add", "prune", "bundle",
    }


@pytest.mark.parametrize("args", [
    ["sync", "--quiet"],
    ["sync", "--quiet", "--dry-run"],
    ["dry-run"],
    ["publish", "--quiet"],
    ["manage", "--quiet"],
])
def test_commands_fail_cleanly_without_config(args, tmp_path, monkeypatch):
    """Without mmw.toml every headless command must exit non-zero, not crash."""
    monkeypatch.chdir(tmp_path)
    from mmw.cli import app
    result = runner.invoke(app, args)
    assert result.exit_code != 0


def test_publish_dry_run_skips_bundle_only_network_without_crashing(tmp_path, monkeypatch):
    """IMPORTANT 3 (whole-branch review) — `mmw publish --dry-run` iterates
    every configured network when `-n` is omitted. A bundle-only network
    (root=None) used to reach `publish_base()` -> `ValueError` with no guard
    in `dry_run_network`, aborting the whole command with an uncaught
    traceback -- one misconfigured-for-publish network took down the dry-run
    report for every other network too. `cmd_publish` also used to print the
    literal string "None" as the destination for such a network."""
    from mmw.core import config
    monkeypatch.chdir(tmp_path)

    cfg = config.Config(
        settings=config.Settings(mirror_dir=tmp_path / "mirror"),
        networks=[
            config.NetworkConfig(name="bundle-only", bundle_dir=tmp_path / "out"),
            config.NetworkConfig(name="published", root=tmp_path / "srv"),
        ],
    )
    config.save(cfg, tmp_path)

    from mmw.cli import app
    result = runner.invoke(app, ["publish", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
    assert "None" not in result.output
    assert "Skipping network: bundle-only" in result.output
    assert "Publishing network: published" in result.output
