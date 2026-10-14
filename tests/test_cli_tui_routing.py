"""Every TUI command opens MmwApp at the right section."""

import pytest
from typer.testing import CliRunner

from mmw import cli
from mmw.core import config
from mmw.tui.app import MmwApp

SRC = "https://deb.example.com/debian"


@pytest.fixture
def captured(monkeypatch):
    apps = []
    monkeypatch.setattr(cli, "_run_tui", lambda app: apps.append(app))
    monkeypatch.setattr(cli, "_CFG_DIR", cli._CFG_DIR)     # --config sets it; restored after
    return apps


def _cfg(tmp_path):
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "pool"),
                        sources=[config.SourceConfig(url=SRC, suites=["bookworm"],
                                                     components=["main"], arches=["amd64"])])
    config.save(cfg, tmp_path)
    return cfg


def _invoke(tmp_path, *args):
    return CliRunner().invoke(cli.app, ["--config", str(tmp_path), *args])


@pytest.mark.parametrize("args,section", [(["manage"], "repositories"),
                                          (["publish"], "networks")])
def test_manage_and_publish_open_their_section(tmp_path, captured, args, section):
    _cfg(tmp_path)
    result = _invoke(tmp_path, *args)
    assert result.exit_code == 0, result.output
    assert len(captured) == 1 and isinstance(captured[0], MmwApp)
    assert captured[0]._initial_section == section


def test_init_without_a_config_opens_setup(tmp_path, captured):
    result = _invoke(tmp_path, "init", "--mirror-dir", str(tmp_path / "pool"))
    assert result.exit_code == 0, result.output
    app = captured[0]
    assert app.cfg is None and app._initial_section == "setup"
    assert app.setup_defaults["mirror_dir"] == tmp_path / "pool"


def test_init_with_a_config_opens_repositories_and_force_opens_setup(tmp_path, captured):
    _cfg(tmp_path)
    _invoke(tmp_path, "init")
    _invoke(tmp_path, "init", "--force")
    assert [a._initial_section for a in captured] == ["repositories", "setup"]


def test_sync_opens_the_sync_section_before_resolution(tmp_path, captured):
    _cfg(tmp_path)
    result = _invoke(tmp_path, "sync", "--dry-run")
    assert result.exit_code == 0, result.output
    app = captured[0]
    assert app._initial_section == "sync"
    assert app._start_sync_selection == (([], []), True)
    assert app._start_sync_with is None


def test_the_development_ui_command_is_gone(tmp_path, captured):
    _cfg(tmp_path)
    assert _invoke(tmp_path, "ui").exit_code != 0
