"""`[settings] theme` — the TUI colour theme, stored with the other settings."""
from pathlib import Path

from mmw.core import config


def _cfg(theme=None):
    s = {"mirror_dir": Path("/srv/pool"), "max_connections": 4}
    if theme is not None:
        s["theme"] = theme
    return config.Config(settings=config.Settings(**s))


def test_default_theme_is_mmw():
    assert config.DEFAULT_THEME == "mmw"
    assert _cfg().settings.theme == "mmw"


def test_default_theme_is_not_written(tmp_path):
    config.save(_cfg(), tmp_path)
    assert "theme" not in config.config_path(tmp_path).read_text()


def test_chosen_theme_round_trips(tmp_path):
    config.save(_cfg("gruvbox"), tmp_path)
    assert config.load(tmp_path).settings.theme == "gruvbox"


def test_existing_file_without_theme_still_loads(tmp_path):
    config.config_path(tmp_path).write_text(
        '[settings]\nmirror_dir = "/srv/pool"\nmax_connections = 4\n')
    assert config.load(tmp_path).settings.theme == "mmw"
