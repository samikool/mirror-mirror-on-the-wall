"""Adopting from the far-side TUI: A, a folder, where it is served, Look,
Adopt — the same steps as `mmw site adopt`."""
import asyncio

from textual.widgets import Input

from mmw.site import home as H
from mmw.site import recents

from apt_tree import make_tree
from waiting import until


def test_adopt_from_the_tui(tmp_path, monkeypatch):
    from mmw.site.mset import open_set
    from mmw.tui.site.app import SiteApp
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "srv"
    H.init_root(root, name="main", set_id="s1", network="oak")
    recents.remember(str(root))
    src = make_tree(tmp_path / "old")

    async def go():
        app = SiteApp(open_set())
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage")
            await pilot.press("A")
            await until(pilot, lambda: type(app.screen).__name__ == "AdoptPage" and app.screen.query("#adopt-path"))
            app.screen.query_one("#adopt-path", Input).value = str(src)
            app.screen.query_one("#adopt-as", Input).value = "deb.debian.org/debian"
            from textual.widgets import Checkbox
            app.screen.query_one("#adopt-unverified", Checkbox).value = True
            app.screen.query_one("#btn-adopt-look").press()
            await until(pilot, lambda: "Found 1 tree" in str(app.screen.query_one("#adopt-report").render()))
            app.screen.query_one("#btn-adopt-go").press()
            await until(pilot, lambda: type(app.screen).__name__ == "AdoptRunPage" and app.screen.finished)
            assert "adopted" in str(app.screen.query_one("#adopt-out").render())
    asyncio.run(go())
    assert (root.resolve() / "deb.debian.org/debian/dists/bookworm/Release").exists()
