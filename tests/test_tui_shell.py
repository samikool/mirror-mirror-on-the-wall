"""The single-app shell: sections, sidebar, breadcrumbs, Esc, narrow layout."""
import asyncio
from pathlib import Path

import pytest
from textual.theme import BUILTIN_THEMES

from mmw.core import config
from mmw.tui.app import MmwApp
from mmw.tui.navigation import SECTION_MODES
from mmw.tui.theme import MMW_THEMES
from mmw.tui.ui.page import Page
from mmw.tui.ui.sidebar import SectionLink, Sidebar
from waiting import until


def _cfg():
    return config.Config(settings=config.Settings(mirror_dir=Path("/srv/pool"),
                                                  max_connections=8))


class _Sub(Page):
    TITLE = "lab"


async def _settle(pilot, n=3):
    for _ in range(n):
        await pilot.pause()


def _run(check, tmp_path, section="repositories", size=(120, 40)):
    async def go():
        app = MmwApp(_cfg(), tmp_path, initial_section=section)
        async with app.run_test(size=size) as pilot:
            await _settle(pilot)
            await check(app, pilot)
    asyncio.run(go())


def _active(app):
    return [link.mode for link in app.screen.query(SectionLink) if link.has_class("-active")]


def test_opens_at_the_requested_section(tmp_path):
    async def check(app, pilot):
        assert app.current_mode == "settings"
        assert _active(app) == ["settings"]
    _run(check, tmp_path, section="settings")


def test_unknown_section_is_refused(tmp_path):
    with pytest.raises(ValueError, match="bogus"):
        MmwApp(_cfg(), tmp_path, initial_section="bogus")


def test_number_keys_switch_sections(tmp_path):
    async def check(app, pilot):
        for key, mode in [("2", "networks"), ("3", "sync"), ("4", "publish"), ("5", "settings"), ("1", "repositories")]:
            await pilot.press(key)
            await _settle(pilot)
            assert app.current_mode == mode
            assert _active(app) == [mode]
    _run(check, tmp_path)


def test_clicking_a_section_link_switches(tmp_path):
    async def check(app, pilot):
        await pilot.click("#nav-sync")
        await _settle(pilot)
        assert app.current_mode == "sync"
    _run(check, tmp_path)


def test_breadcrumb_follows_the_stack_and_esc_goes_back(tmp_path):
    async def check(app, pilot):
        app.push_screen(_Sub())
        await _settle(pilot)
        assert str(app.screen.query_one("#breadcrumb").render()) == "Settings › lab"
        await pilot.press("escape")
        await _settle(pilot)
        assert app.screen.TITLE == "Settings"
    _run(check, tmp_path, section="settings")


def test_esc_on_a_section_root_does_nothing(tmp_path):
    async def check(app, pilot):
        await pilot.press("escape")
        await _settle(pilot)
        assert app.screen.TITLE == "Settings" and len(app.screen_stack) == 1
    _run(check, tmp_path, section="settings")


def test_each_section_keeps_its_own_stack(tmp_path):
    async def check(app, pilot):
        app.push_screen(_Sub())
        await _settle(pilot)
        await pilot.press("1")
        await _settle(pilot)
        await pilot.press("5")
        await _settle(pilot)
        assert app.screen.TITLE == "lab"
    _run(check, tmp_path, section="settings")


def test_sidebar_collapses_to_a_tab_strip_and_back(tmp_path):
    async def check(app, pilot):
        def sidebar():
            return app.screen.query_one(Sidebar)
        assert not app.screen.has_class("-narrow") and sidebar().region.height > 1
        await pilot.resize_terminal(80, 24)
        await _settle(pilot)
        assert app.screen.has_class("-narrow") and sidebar().region.height == 1
        # a section first shown after the resize is laid out for the new width
        await pilot.press("2")
        await _settle(pilot)
        assert app.screen.has_class("-narrow")
        await pilot.resize_terminal(120, 40)
        await _settle(pilot)
        assert not app.screen.has_class("-narrow") and sidebar().region.height > 1
        await pilot.press("1")
        await _settle(pilot)
        assert not app.screen.has_class("-narrow")
    _run(check, tmp_path)


def test_status_line_kinds(tmp_path):
    async def check(app, pilot):
        app.screen.set_status("saved", "ok")
        await _settle(pilot)
        status = app.screen.query_one("#page-status")
        assert status.has_class("-ok") and str(status.render()) == "saved"
        app.screen.set_status("[not markup]", "error")
        await _settle(pilot)
        assert status.has_class("-error") and not status.has_class("-ok")
        assert str(status.render()) == "[not markup]"
    _run(check, tmp_path)


def _app_with_notes(cfg, tmp_path, **kw):
    app = MmwApp(cfg, tmp_path, **kw)
    app.notes = []
    app.notify = lambda message, **_: app.notes.append(message)
    return app


def _run_app(app, check, size=(120, 40)):
    async def go():
        async with app.run_test(size=size) as pilot:
            await _settle(pilot)
            await check(app, pilot)
    asyncio.run(go())


def test_configured_theme_is_applied(tmp_path):
    cfg = _cfg()
    cfg.settings.theme = "nord"
    async def check(app, pilot):
        assert app.theme == "nord"
    _run_app(_app_with_notes(cfg, tmp_path), check)


def test_unknown_theme_falls_back_without_rewriting_the_config(tmp_path):
    cfg = _cfg()
    cfg.settings.theme = "gone"
    config.save(cfg, tmp_path)
    before = config.config_path(tmp_path).read_text()
    async def check(app, pilot):
        assert app.theme == "mmw"
        assert len([n for n in app.notes if "gone" in n]) == 1
        await app.workers.wait_for_complete()
        assert config.config_path(tmp_path).read_text() == before
    _run_app(_app_with_notes(cfg, tmp_path), check)


def test_user_theme_files_load_and_bad_ones_are_reported(tmp_path):
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "ocean.toml").write_text('primary = "#336699"\n')
    (themes / "broken.toml").write_text('primary = "nope"\n')
    cfg = _cfg()
    cfg.settings.theme = "ocean"
    async def check(app, pilot):
        assert app.theme == "ocean"
        assert "broken" not in app.available_themes
        assert any(n.startswith("Theme file skipped — broken.toml") for n in app.notes)
    _run_app(_app_with_notes(cfg, tmp_path), check)


def test_changing_theme_persists_only_the_theme(tmp_path):
    cfg = _cfg()
    config.save(cfg, tmp_path)
    async def check(app, pilot):
        app.theme = "gruvbox"          # what the command palette does
        await _settle(pilot)
        await app.workers.wait_for_complete()
        saved = config.load(tmp_path).settings
        assert saved.theme == "gruvbox"
        assert saved.mirror_dir == Path("/srv/pool") and saved.max_connections == 8
        assert app.cfg.settings.theme == "gruvbox"
    _run_app(_app_with_notes(cfg, tmp_path), check)


def test_failed_theme_save_is_reported_and_config_unchanged(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only file system")
    monkeypatch.setattr(config, "save", boom)
    async def check(app, pilot):
        app.theme = "gruvbox"
        await _settle(pilot)
        await app.workers.wait_for_complete()
        assert app.cfg.settings.theme == "mmw"
        assert any("read-only" in n for n in app.notes)
    _run_app(_app_with_notes(_cfg(), tmp_path), check)


@pytest.mark.parametrize("theme", sorted({*BUILTIN_THEMES, *(t.name for t in MMW_THEMES)}))
def test_every_theme_renders_every_section(tmp_path, theme):
    cfg = _cfg()
    cfg.settings.theme = theme
    async def check(app, pilot):
        assert app.theme == theme
        for mode in SECTION_MODES:
            await app.switch_mode(mode)
            await _settle(pilot)
            app.screen.query_one("#page-body")   # composed and styled without error
    _run_app(_app_with_notes(cfg, tmp_path), check)


@pytest.mark.parametrize("section", SECTION_MODES)
def test_starting_section_builds_completely_under_app_run(tmp_path, section):
    """`app.run()` — how `mmw ui` starts — builds the first section's page
    before any screen is on the stack, unlike `run_test`. An Input given a
    starting value asks the app for its screen while composing, so the
    Settings fields used to fail to build and left blank rows (caught by the
    snapshot review, not by any pilot test)."""
    app = MmwApp(_cfg(), tmp_path, initial_section=section)

    async def auto_pilot(pilot):
        await _settle(pilot)
        app.exit((app.current_mode,
                  [(i.id, i.value) for i in app.screen.query("Input")]))

    mode, inputs = app.run(headless=True, auto_pilot=auto_pilot, size=(100, 32))
    assert app.return_code == 0
    assert mode == section
    if section == "settings":
        assert inputs == [("settings-mirror-dir", "/srv/pool"),
                          ("settings-max-connections", "8")]


def test_page_hint_line_follows_focus(tmp_path):
    async def check(app, pilot):
        hint = app.screen.query_one("#page-hint")
        assert hint.region.height == 1
        app.screen.query_one("#settings-max-connections").focus()
        await _settle(pilot)
        assert "Simultaneous downloads" in str(hint.render())
        app.screen.query_one("#btn-settings-save").focus()
        await _settle(pilot)
        assert str(hint.render()) == ""
    _run(check, tmp_path, section="settings")


class _Result(Page):
    TITLE = "pick"

    def compose_actions(self):
        from mmw.tui.ui.widgets import Button
        yield Button("Done", id="btn-done", compact=True)

    def on_button_pressed(self, event):
        self.dismiss("chosen")


def test_a_pushed_page_returns_its_result_and_none_on_esc(tmp_path):
    async def check(app, pilot):
        got = []

        async def ask():
            got.append(await app.push_screen_wait(_Result()))
        app.run_worker(ask())
        await _settle(pilot)
        app.screen.query_one("#btn-done").press()
        await _settle(pilot)
        app.run_worker(ask())
        await _settle(pilot)
        await pilot.press("escape")
        await _settle(pilot)
        assert got == ["chosen", None]
        assert app.screen.TITLE == "Settings"
    _run(check, tmp_path, section="settings")


def test_back_is_not_advertised_at_a_section_root(tmp_path):
    async def check(app, pilot):
        assert app.screen.check_action("back", ()) is False
        app.push_screen(_Sub())
        await _settle(pilot)
        assert app.screen.check_action("back", ()) is True
    _run(check, tmp_path, section="settings")


@pytest.mark.parametrize("section", ["repositories", "networks", "sync", "settings"])
def test_the_footer_never_offers_back_at_a_section_root(tmp_path, section):
    """check_action answered right, but the footer was built while the mode
    switch was still landing and kept advertising `esc Back` on Repositories."""
    async def check(app, pilot):
        keys = [str(k.render()) for k in app.screen.query("Footer FooterKey")]
        assert keys and not any("Back" in k for k in keys), keys
    _run(check, tmp_path, section=section)


@pytest.mark.parametrize("section", ["repositories", "networks", "sync", "settings"])
def test_the_footer_fits_at_80_columns(tmp_path, section):
    """A key pushed off the right edge of the footer is undiscoverable; `?`
    lists every key, so the footer keeps the page's verbs and Help."""
    async def go():
        cfg = _cfg()
        config.save(cfg, tmp_path)
        app = MmwApp(cfg, tmp_path, initial_section=section)
        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            footer = app.screen.query_one("Footer")
            keys = " ".join(str(k.render()) for k in footer.query("FooterKey"))
            assert "Help" in keys
            assert footer.virtual_size.width <= footer.size.width, keys
    asyncio.run(go())


def test_a_page_in_a_plain_themed_app_has_no_sidebar():
    from mmw.tui.ui.base import ThemedApp

    class _Host(ThemedApp):
        def on_mount(self):
            self.push_screen(_Sub())

    async def go():
        app = _Host()
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)
            assert not app.screen.query(Sidebar)
            assert app.screen.query_one("#page-body")
    asyncio.run(go())


def test_non_scrolling_body_and_subtitle(tmp_path):
    from textual.containers import VerticalScroll

    class _Fixed(Page):
        TITLE = "fixed"
        SCROLLS = False
        subtitle = "https://deb.example.com/debian"

    async def check(app, pilot):
        app.push_screen(_Fixed())
        await _settle(pilot)
        assert not isinstance(app.screen.query_one("#page-body"), VerticalScroll)
        assert str(app.screen.query_one("#page-subtitle").render()) == "https://deb.example.com/debian"
    _run(check, tmp_path, section="settings")


def test_save_config_serialises_with_theme_writes(tmp_path, monkeypatch):
    import time
    real = config.save

    def slow(cfg, d):
        time.sleep(0.2)
        return real(cfg, d)
    cfg = _cfg()
    config.save(cfg, tmp_path)
    monkeypatch.setattr(config, "save", slow)

    async def check(app, pilot):
        app.theme = "nord"
        await pilot.pause()
        src = config.SourceConfig(url="https://deb.example.com/debian", arches=["amd64"])
        await app.save_config(lambda c: c.model_copy(update={"sources": [*c.sources, src]}))
        # The theme's own save starts from a signal handler: under load it
        # may not have begun when wait_for_complete runs. Wait for the file.
        await until(pilot, lambda: config.load(tmp_path).settings.theme == "nord")
        await app.workers.wait_for_complete()
        saved = config.load(tmp_path)
        assert saved.settings.theme == "nord"
        assert [s.url for s in saved.sources] == ["https://deb.example.com/debian"]
    _run_app(_app_with_notes(cfg, tmp_path), check)


def test_a_cancelled_write_leaves_disk_and_memory_agreeing(tmp_path, monkeypatch):
    import time
    real = config.save

    def slow(cfg, d):
        time.sleep(0.3)
        return real(cfg, d)
    cfg = _cfg()
    config.save(cfg, tmp_path)
    monkeypatch.setattr(config, "save", slow)

    async def check(app, pilot):
        src = config.SourceConfig(url="https://deb.example.com/debian", arches=["amd64"])
        worker = app.run_worker(app.save_config(
            lambda c: c.model_copy(update={"sources": [*c.sources, src]})))
        await pilot.pause(0.1)
        worker.cancel()
        await pilot.pause(0.5)
        on_disk = [s.url for s in config.load(tmp_path).sources]
        in_memory = [s.url for s in app.cfg.sources]
        assert on_disk == in_memory
    _run_app(_app_with_notes(cfg, tmp_path), check)
