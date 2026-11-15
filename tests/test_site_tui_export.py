"""Exporting from the site TUI: x opens the page, a target is picked from
those sent to before or typed, and the bundle is written on a progress page."""
import asyncio

import pytest
from textual.widgets import Input, Select, SelectionList, Static

from mmw.site.mset import open_set
from mmw.tui.site.app import SiteApp

from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_export_write import one_root  # noqa: F401
from waiting import until


@pytest.fixture(autouse=True)
def _xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _run(check):
    async def go():
        app = SiteApp(open_set())
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage")
            await check(app, pilot)
    asyncio.run(go())


def _name(app):
    return type(app.screen).__name__


def _would(app) -> str:
    return str(app.screen.query_one("#export-would", Static).render())


async def _open(app, pilot):
    await until(pilot, lambda: app.screen.query("#site-repos") and
                app.screen.query_one("#site-repos").row_count == 2)
    await pilot.press("x")
    await until(pilot, lambda: _name(app) == "ExportPage" and app.screen.query("#export-repos"))


def test_export_from_the_tui(one_root, tmp_path):  # noqa: F811
    out = tmp_path / "to-elm.tar"

    async def check(app, pilot):
        await _open(app, pilot)
        repos = app.screen.query_one("#export-repos", SelectionList)
        assert len(repos.selected) == 2                                  # every repository, ticked
        app.screen.query_one("#export-target", Input).value = "elm"
        await until(pilot, lambda: "first export to 'elm'" in _would(app) and "2 point(s)" in _would(app))
        app.screen.query_one("#export-out", Input).value = str(out)
        app.screen.query_one("#btn-export-go").press()
        await until(pilot, lambda: _name(app) == "ExportRunPage" and app.screen.finished, timeout=30)
        assert "wrote" in str(app.screen.query_one("#run-done").render())
    _run(check)
    assert out.exists()


def test_a_known_target_with_nothing_new_says_so(one_root, tmp_path):  # noqa: F811
    from mmw.site.export import export_bundle
    export_bundle(open_set([str(one_root)]), "elm", str(tmp_path / "first.tar"))

    async def check(app, pilot):
        await _open(app, pilot)
        known = app.screen.query_one("#export-known", Select)
        assert known.value == "elm"                                      # picked from those sent to before
        await until(pilot, lambda: "nothing new for elm" in _would(app))
        app.screen.query_one("#btn-export-go").press()
        await until(pilot, lambda: "nothing new for elm" in str(app.screen.query_one("#page-status").render()))
        assert _name(app) == "ExportPage"
    _run(check)
