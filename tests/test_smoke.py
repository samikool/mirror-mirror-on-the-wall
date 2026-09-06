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


@pytest.mark.parametrize("cmd", ["init", "manage", "sync", "dry-run", "publish", "prune"])
def test_command_help(cmd):
    from mmw.cli import app
    result = runner.invoke(app, [cmd, "--help"])
    assert result.exit_code == 0


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
