"""Settings, rebuilt as a section page."""
import asyncio
from pathlib import Path

from textual.widgets import Button, Input, Select

from mmw.core import config
from mmw.tui.app import MmwApp
from waiting import until


def _cfg():
    return config.Config(settings=config.Settings(mirror_dir=Path("/srv/pool"),
                                                  max_connections=8))


async def _settle(pilot, n=4):
    for _ in range(n):
        await pilot.pause()


def _run(tmp_path, check, cfg=None):
    cfg = cfg or _cfg()
    config.save(cfg, tmp_path)

    async def go():
        app = MmwApp(cfg, tmp_path, initial_section="settings")
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)
            await check(app, pilot)
    asyncio.run(go())


async def _save(app, pilot):
    app.screen.query_one("#btn-settings-save", Button).press()
    await _settle(pilot)
    await app.workers.wait_for_complete()
    await _settle(pilot)


def _status(app):
    return str(app.screen.query_one("#page-status").render())


def test_shows_current_values(tmp_path):
    async def check(app, pilot):
        s = app.screen
        assert s.query_one("#settings-mirror-dir", Input).value == "/srv/pool"
        assert s.query_one("#settings-max-connections", Input).value == "8"
        assert s.query_one("#settings-theme", Select).value == "mmw"
    _run(tmp_path, check)


def test_save_writes_both_settings(tmp_path):
    async def check(app, pilot):
        app.screen.query_one("#settings-mirror-dir", Input).value = "/data/pool"
        app.screen.query_one("#settings-max-connections", Input).value = "32"
        await _save(app, pilot)
        saved = config.load(tmp_path).settings
        assert (saved.mirror_dir, saved.max_connections) == (Path("/data/pool"), 32)
        assert app.cfg.settings == saved
        assert app.screen.query_one("#page-status").has_class("-ok")
    _run(tmp_path, check)


def test_invalid_values_are_refused_and_nothing_is_written(tmp_path):
    async def check(app, pilot):
        before = config.config_path(tmp_path).read_text()
        app.screen.query_one("#settings-max-connections", Input).value = "0"
        await _save(app, pilot)
        assert "1 or more" in _status(app)
        app.screen.query_one("#settings-max-connections", Input).value = "4"
        app.screen.query_one("#settings-mirror-dir", Input).value = ""
        await _save(app, pilot)
        assert "cannot be empty" in _status(app)
        assert config.config_path(tmp_path).read_text() == before
        assert app.cfg.settings.max_connections == 8
    _run(tmp_path, check)


def test_failed_write_leaves_config_unchanged(tmp_path, monkeypatch):
    async def check(app, pilot):
        def boom(*a, **k):
            raise OSError("read-only file system")
        monkeypatch.setattr(config, "save", boom)
        app.screen.query_one("#settings-max-connections", Input).value = "32"
        await _save(app, pilot)
        assert "read-only" in _status(app)
        assert app.screen.query_one("#page-status").has_class("-error")
        assert app.cfg.settings.max_connections == 8
    _run(tmp_path, check)


def test_theme_applies_and_saves_at_once_without_unsaved_edits(tmp_path):
    async def check(app, pilot):
        app.screen.query_one("#settings-mirror-dir", Input).value = "/half/typed"
        app.screen.query_one("#settings-theme", Select).value = "nord"
        await _settle(pilot)
        await app.workers.wait_for_complete()
        assert app.theme == "nord"
        saved = config.load(tmp_path).settings
        assert saved.theme == "nord"
        assert saved.mirror_dir == Path("/srv/pool")      # the unsaved edit stayed unsaved
        assert app.screen.query_one("#settings-mirror-dir", Input).value == "/half/typed"
    _run(tmp_path, check)


def test_revert_restores_saved_values(tmp_path):
    async def check(app, pilot):
        app.screen.query_one("#settings-mirror-dir", Input).value = "/elsewhere"
        app.screen.query_one("#btn-settings-revert", Button).press()
        await _settle(pilot)
        assert app.screen.query_one("#settings-mirror-dir", Input).value == "/srv/pool"
    _run(tmp_path, check)


def test_theme_picker_follows_the_command_palette(tmp_path):
    async def check(app, pilot):
        await pilot.press("1")
        await _settle(pilot)
        app.theme = "dracula"
        await _settle(pilot)
        await pilot.press("5")
        await _settle(pilot)
        assert app.screen.query_one("#settings-theme", Select).value == "dracula"
    _run(tmp_path, check)


# ── Overlapping saves (final review #1) and the fallback theme (#2) ─────────

def _slow_save(monkeypatch, delay=0.3):
    import time
    real = config.save

    def slow(cfg, directory):
        time.sleep(delay)        # a stalled write on a busy disk
        return real(cfg, directory)
    monkeypatch.setattr(config, "save", slow)


async def _until(pilot, cond, tries=None):
    """`tries` is ignored: see tests/waiting.py — wait for the thing, against a clock."""
    await until(pilot, cond)

def test_save_during_a_theme_write_keeps_both(tmp_path, monkeypatch):
    async def check(app, pilot):
        _slow_save(monkeypatch)
        app.screen.query_one("#settings-theme", Select).value = "nord"
        await pilot.pause()
        app.screen.query_one("#settings-max-connections", Input).value = "32"
        app.screen.query_one("#btn-settings-save", Button).press()
        await _until(pilot, lambda: "Saved" in _status(app))
        await app.workers.wait_for_complete()
        saved = config.load(tmp_path).settings
        assert (saved.theme, saved.max_connections) == ("nord", 32)
        assert (app.cfg.settings.theme, app.cfg.settings.max_connections) == ("nord", 32)
    _run(tmp_path, check)


def test_quick_theme_changes_end_on_the_last_one(tmp_path, monkeypatch):
    async def check(app, pilot):
        _slow_save(monkeypatch)
        app.theme = "nord"
        await pilot.pause()
        app.theme = "mmw"
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause(0.5)
        await app.workers.wait_for_complete()
        assert config.load(tmp_path).settings.theme == "mmw"
        assert app.cfg.settings.theme == "mmw"
    _run(tmp_path, check)


def test_save_after_a_theme_fallback_writes_the_theme_shown(tmp_path):
    cfg = _cfg()
    cfg.settings.theme = "gone"

    async def check(app, pilot):
        assert app.theme == "mmw"
        await _save(app, pilot)
        assert config.load(tmp_path).settings.theme == "mmw"
    _run(tmp_path, check, cfg=cfg)


def test_pool_directory_can_be_browsed(tmp_path):
    (tmp_path / "pools" / "big").mkdir(parents=True)

    async def check(app, pilot):
        app.screen.query_one("#settings-mirror-dir-browse", Button).press()
        await _settle(pilot)
        page = app.screen
        assert page.TITLE == "Choose the pool directory"
        page.query_one("#path-input", Input).value = str(tmp_path / "pools" / "big")
        page.query_one("#btn-use", Button).press()
        await _settle(pilot)
        assert app.screen.query_one("#settings-mirror-dir", Input).value == str(tmp_path / "pools" / "big")
    _run(tmp_path, check)
