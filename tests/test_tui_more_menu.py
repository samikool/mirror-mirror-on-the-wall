"""More…: a page's actions with no button of their own, listed with their keys —
nothing in the TUI is reachable only by a key nobody can see."""
import asyncio

import pytest
from textual.widgets import OptionList

from mmw.core import config
from mmw.tui.sections.networks import NetworksPage
from mmw.tui.sections.repositories import RepositoriesPage
from mmw.tui.site.home import SiteHomePage
from mmw.tui.ui.page import Page
from waiting import settle, until

PAGES = (RepositoriesPage, NetworksPage, SiteHomePage)


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.__name__)
def test_every_more_entry_is_a_bound_action(page):
    actions = {b.action for b in page.BINDINGS}
    assert page.MORE and set(page.MORE) <= actions
    for name in page.MORE:
        assert hasattr(page, f"action_{name}"), name


def test_more_lists_the_extras_and_runs_one(tmp_path):
    from mmw.tui.app import MmwApp
    from mmw.tui.packages import AddPackagesPage
    from mmw.tui.ui.dialog import MoreMenu
    from mmw.tui.ui.widgets import Button
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"),
                        sources=[config.SourceConfig(url="https://deb.debian.org/debian", suites=["bookworm"],
                                                     components=["main"], arches=["amd64"])])
    config.save(cfg, tmp_path)

    async def scenario():
        app = MmwApp(cfg, tmp_path, initial_section="repositories")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            app.screen.query_one("#btn-page-more", Button).press()
            await until(pilot, lambda: isinstance(app.screen, MoreMenu))
            menu = app.screen
            options = menu.query_one("#more-list", OptionList)
            labels = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
            assert [x.split()[0] for x in labels] == ["Add", "Sources", "Add", "History", "Keep", "Merge"]
            assert any("Add a past point…" in x and x.endswith("[dim]t[/]") for x in labels)
            # The sources file is only a folder repository's: shown, but not offered here.
            assert options.get_option("sources_file").disabled
            assert not options.get_option("add_packages").disabled
            options.highlighted = 0
            await pilot.press("enter")
            await until(pilot, lambda: isinstance(app.screen, AddPackagesPage))
    asyncio.run(scenario())


def test_escape_closes_the_menu_and_does_nothing(tmp_path):
    from mmw.tui.app import MmwApp
    from mmw.tui.ui.dialog import MoreMenu
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"))
    config.save(cfg, tmp_path)

    async def scenario():
        app = MmwApp(cfg, tmp_path, initial_section="networks")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            page = app.screen
            assert isinstance(page, Page) and page.query("#btn-page-more")
            page.action_more()
            await until(pilot, lambda: isinstance(app.screen, MoreMenu))
            await pilot.press("escape")
            await until(pilot, lambda: app.screen is page)
    asyncio.run(scenario())
