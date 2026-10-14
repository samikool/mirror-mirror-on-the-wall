"""The Add wizard: pick what you are adding, then that provider's step."""
import asyncio

from textual.app import ComposeResult
from textual.widgets import Input, OptionList, TextArea

from mmw.tui.add_source import AddSourcePage
from mmw.tui.ui.base import ThemedApp
from mmw.tui.custom_repo import StepCustom
from mmw.tui.provider_ui import add_entries
from waiting import until


class _Harness(ThemedApp):
    """mmw's own app base, so pages are laid out by mmw.tcss as users see them."""

    def __init__(self, page_factory):
        super().__init__()
        self._factory = page_factory
        self.result = "unset"

    def compose(self) -> ComposeResult:
        yield from ()

    def on_mount(self) -> None:
        self.push_screen(self._factory(), lambda r: setattr(self, "result", r))


def test_entries_are_providers_with_an_add_step_then_detect():
    entries = add_entries()
    assert [e.key for e in entries] == ["apt", "vscode", "detect"]
    assert entries[0].label == "APT repository"
    assert entries[1].label == "VS Code"
    assert entries[2].label == "Detect from URL or paste…"
    assert [e.step_title for e in entries] == ["Repository", "VS Code", "Repository"]


def test_picker_comes_first_and_lists_every_entry():
    async def scenario():
        app = _Harness(AddSourcePage)
        async with app.run_test(size=(80, 24)) as pilot:
            await until(pilot, lambda: app.screen.query("#add-entry-list"))
            lst = app.screen.query_one("#add-entry-list", OptionList)
            assert lst.option_count == len(add_entries())
            # Every line of every entry is on screen: nothing to scroll to.
            assert lst.virtual_size.height <= lst.region.height
            # …and the whole list sits on screen, above the action row.
            actions_top = app.screen.query_one("#btn-wizard-next").region.y
            assert lst.region.y + lst.region.height <= actions_top
            assert app.screen.STEPS == ("What", "Repository", "Confirm")

    asyncio.run(scenario())


def test_enter_on_an_entry_advances_to_its_step():
    async def scenario():
        app = _Harness(AddSourcePage)
        async with app.run_test(size=(100, 32)) as pilot:
            await until(pilot, lambda: app.screen.query("#add-entry-list"))
            app.screen.query_one("#add-entry-list", OptionList).focus()
            await pilot.press("enter")
            await until(pilot, lambda: app.screen.step == 1)
            assert app.screen.query_one(StepCustom).display

    asyncio.run(scenario())


def test_back_and_next_keep_the_step_as_left():
    async def scenario():
        app = _Harness(AddSourcePage)
        async with app.run_test(size=(100, 32)) as pilot:
            await until(pilot, lambda: app.screen.query("#add-entry-list"))
            page = app.screen
            page.query_one("#btn-wizard-next").press()
            await until(pilot, lambda: page.step == 1)
            page.query_one("#custom-url", Input).value = "http://example.com/ubuntu"
            page.query_one("#btn-wizard-back").press()
            await until(pilot, lambda: page.step == 0)
            page.query_one("#btn-wizard-next").press()
            await until(pilot, lambda: page.step == 1)
            assert page.query_one("#custom-url", Input).value == "http://example.com/ubuntu"
            assert len(page.query(StepCustom)) == 1

    asyncio.run(scenario())


def test_cancel_on_the_picker_dismisses_none():
    async def scenario():
        app = _Harness(AddSourcePage)
        async with app.run_test(size=(100, 32)) as pilot:
            await until(pilot, lambda: app.screen.query("#add-entry-list"))
            app.screen.query_one("#btn-wizard-cancel").press()
            await until(pilot, lambda: app.result != "unset")
            assert app.result is None

    asyncio.run(scenario())


def test_a_fixed_entry_skips_the_picker():
    async def scenario():
        app = _Harness(lambda: AddSourcePage(entry="apt"))
        async with app.run_test(size=(100, 32)) as pilot:
            await until(pilot, lambda: app.screen.query(StepCustom))
            page = app.screen
            assert page.STEPS == ("Repository", "Confirm")
            assert not page.query("#add-entry-list")
            assert page.focused is page.query_one("#custom-url", Input) or \
                page.focused is page.query_one("#paste-area", TextArea)

    asyncio.run(scenario())


def test_edit_goes_through_the_provider_registry():
    import inspect
    from mmw.core import config
    from mmw.tui import provider_ui
    from mmw.tui.sections import repositories as page
    from mmw.tui.source_screens import EditSourcePage
    assert "EditSourcePage(" not in inspect.getsource(page)
    src = config.SourceConfig(url="https://x.example/repo/arm64", provider="apt-flat")
    assert isinstance(provider_ui.edit_page(src, set(), set()), EditSourcePage)


def test_an_unknown_entry_is_a_clear_error():
    import pytest
    with pytest.raises(ValueError, match="no Add entry 'nope'"):
        AddSourcePage(entry="nope")
