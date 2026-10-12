"""The dimension picker: range selection, select-all, cancel semantics."""
import asyncio

from textual import work
from textual.app import ComposeResult
from textual.widgets import Button, SelectionList

from mmw.tui.ui.base import ThemedApp
from mmw.tui.pick import PickPage
from waiting import settle, until

OPTIONS = [f"suite-{i}" for i in range(10)]

# A realistic suite list: several families interleaved, as a real archive lists them.
UBUNTU = [
    "focal", "focal-backports", "focal-security", "focal-updates",
    "jammy", "jammy-backports", "jammy-proposed", "jammy-security", "jammy-updates",
    "noble", "noble-security", "noble-updates",
]


class _Harness(ThemedApp):
    def __init__(self, options=None, selected=None):
        super().__init__()
        self._options = options if options is not None else OPTIONS
        self._selected = selected
        self.result = "unset"

    def compose(self) -> ComposeResult:
        yield from ()

    def on_mount(self):
        self._go()

    @work
    async def _go(self):
        self.result = await self.push_screen_wait(
            PickPage("Choose suites", "http://example.com/repo", self._options, self._selected)
        )


async def _settle(pilot, tries: int = 20):
    """Until no work is in flight — see tests/waiting.py. The count is ignored."""
    await settle(pilot)


async def _wait_result(pilot, app, tries: int = 40):
    """`tries` is ignored: wait for the result itself (tests/waiting.py)."""
    await until(pilot, lambda: app.result != "unset")


def _run(scenario):
    asyncio.run(scenario())


def test_nothing_is_selected_by_default():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(100, 32)) as pilot:
            await _settle(pilot)
            assert list(app.screen.query_one("#pick-list", SelectionList).selected) == []

    _run(scenario)


def test_existing_selection_is_restored():
    async def scenario():
        app = _Harness(selected=["suite-2", "suite-5"])
        async with app.run_test(size=(100, 32)) as pilot:
            await _settle(pilot)
            lst = app.screen.query_one("#pick-list", SelectionList)
            assert set(lst.selected) == {"suite-2", "suite-5"}

    _run(scenario)


def test_shift_down_extends_a_range():
    """The behaviour every other list UI has, and SelectionList does not."""
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(100, 32)) as pilot:
            await _settle(pilot)
            lst = app.screen.query_one("#pick-list", SelectionList)
            lst.focus()
            lst.highlighted = 2
            await pilot.pause()
            for _ in range(3):
                await pilot.press("shift+down")
            await _settle(pilot)
            assert set(lst.selected) == {"suite-2", "suite-3", "suite-4", "suite-5"}

    _run(scenario)


def test_shift_up_extends_the_other_way():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(100, 32)) as pilot:
            await _settle(pilot)
            lst = app.screen.query_one("#pick-list", SelectionList)
            lst.focus()
            lst.highlighted = 5
            await pilot.pause()
            for _ in range(2):
                await pilot.press("shift+up")
            await _settle(pilot)
            assert set(lst.selected) == {"suite-3", "suite-4", "suite-5"}

    _run(scenario)


def test_range_extension_stops_at_the_ends():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(100, 32)) as pilot:
            await _settle(pilot)
            lst = app.screen.query_one("#pick-list", SelectionList)
            lst.focus()
            lst.highlighted = 0
            await pilot.pause()
            for _ in range(5):
                await pilot.press("shift+up")       # already at the top
            await _settle(pilot)
            assert set(lst.selected) == {"suite-0"}

    _run(scenario)


def test_select_all_and_clear_buttons():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(100, 32)) as pilot:
            await _settle(pilot)
            screen = app.screen
            lst = screen.query_one("#pick-list", SelectionList)
            screen.query_one("#btn-all", Button).press()
            await _settle(pilot)
            assert len(lst.selected) == len(OPTIONS)
            screen.query_one("#btn-none", Button).press()
            await _settle(pilot)
            assert list(lst.selected) == []

    _run(scenario)


def test_count_tracks_the_selection():
    """Capture what the modal passes to update() rather than poking at Textual
    render internals, which differ between versions."""
    async def scenario():
        seen: list[str] = []
        app = _Harness()
        async with app.run_test(size=(100, 32)) as pilot:
            await _settle(pilot)
            screen = app.screen
            original = screen.query_one("#pick-count").update
            screen.query_one("#pick-count").update = lambda text: (
                seen.append(str(text)), original(text)
            )[1]
            screen.query_one("#pick-list", SelectionList).select("suite-1")
            await _settle(pilot)
            screen.query_one("#btn-all", Button).press()
            await _settle(pilot)

        assert any("1 of 10" in s for s in seen)
        assert any("10 of 10" in s for s in seen)

    _run(scenario)


def test_toggle_then_shift_space_takes_the_whole_range():
    """The pattern: tick the first jammy, move to the last, shift+space.

    Everything between comes in, and nothing outside the span is touched."""
    async def scenario():
        app = _Harness(options=UBUNTU, selected=["focal"])
        async with app.run_test(size=(100, 32)) as pilot:
            await _settle(pilot)
            lst = app.screen.query_one("#pick-list", SelectionList)
            lst.focus()

            lst.highlighted = UBUNTU.index("jammy")
            await pilot.press("space")            # ticks it and drops the anchor
            await _settle(pilot)

            lst.highlighted = UBUNTU.index("jammy-updates")   # move freely
            await pilot.pause()
            await pilot.press("shift+space")
            await _settle(pilot)

            assert set(lst.selected) == {
                "focal",                      # untouched, outside the range
                "jammy", "jammy-backports", "jammy-proposed",
                "jammy-security", "jammy-updates",
            }

    _run(scenario)


def test_shift_space_works_backwards_too():
    async def scenario():
        app = _Harness(options=UBUNTU)
        async with app.run_test(size=(100, 32)) as pilot:
            await _settle(pilot)
            lst = app.screen.query_one("#pick-list", SelectionList)
            lst.focus()
            lst.highlighted = UBUNTU.index("jammy-updates")
            await pilot.press("space")
            await _settle(pilot)
            lst.highlighted = UBUNTU.index("jammy")
            await pilot.pause()
            await pilot.press("shift+space")
            await _settle(pilot)
            assert set(lst.selected) == {
                "jammy", "jammy-backports", "jammy-proposed",
                "jammy-security", "jammy-updates",
            }

    _run(scenario)


def test_a_range_never_unticks_anything():
    """Additive by design — a range must not clear a previous selection."""
    async def scenario():
        app = _Harness(options=UBUNTU, selected=["noble", "noble-updates"])
        async with app.run_test(size=(100, 32)) as pilot:
            await _settle(pilot)
            lst = app.screen.query_one("#pick-list", SelectionList)
            lst.focus()
            lst.highlighted = UBUNTU.index("focal")
            await pilot.press("space")
            await _settle(pilot)
            lst.highlighted = UBUNTU.index("focal-updates")
            await pilot.pause()
            await pilot.press("shift+space")
            await _settle(pilot)
            assert {"noble", "noble-updates"} <= set(lst.selected)

    _run(scenario)


def test_typing_to_jump_then_shift_space():
    """type-to-jump moves the cursor; the anchor must survive it."""
    async def scenario():
        app = _Harness(options=UBUNTU)
        async with app.run_test(size=(100, 32)) as pilot:
            await _settle(pilot)
            lst = app.screen.query_one("#pick-list", SelectionList)
            lst.focus()
            lst.highlighted = UBUNTU.index("focal")
            await pilot.press("space")
            await _settle(pilot)
            await pilot.press("j")                 # jumps to the first "jammy"
            await _settle(pilot)
            await pilot.press("shift+space")
            await _settle(pilot)
            assert set(lst.selected) == {
                "focal", "focal-backports", "focal-security", "focal-updates", "jammy",
            }

    _run(scenario)


def test_done_returns_the_selection():
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(100, 32)) as pilot:
            await _settle(pilot)
            screen = app.screen
            screen.query_one("#pick-list", SelectionList).select("suite-3")
            await pilot.pause()
            screen.query_one("#btn-done", Button).press()
            await _wait_result(pilot, app)
        assert app.result == ["suite-3"]

    _run(scenario)


def test_cancel_returns_none_not_an_empty_list():
    """None means "leave it alone"; [] would mean "deselect everything"."""
    async def scenario():
        app = _Harness(selected=["suite-1"])
        async with app.run_test(size=(100, 32)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#btn-cancel", Button).press()
            await _wait_result(pilot, app)
        assert app.result is None

    _run(scenario)


def test_escape_cancels():
    async def scenario():
        app = _Harness(selected=["suite-1"])
        async with app.run_test(size=(100, 32)) as pilot:
            await _settle(pilot)
            await pilot.press("escape")
            await _wait_result(pilot, app)
        assert app.result is None

    _run(scenario)


def test_layout_holds_at_the_supported_floor():
    """80x24 is the declared minimum; a 55-suite list must still fit."""
    from test_tui_layout import find_problems

    async def scenario():
        app = _Harness(options=[f"s{i}" for i in range(55)])
        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            problems = find_problems(app.screen, 80, 24)
            assert not problems, "\n  ".join(problems)

    _run(scenario)


# ── mouse selection ──────────────────────────────────────────────────────────
#
# The list widget has a top border, so click offset row N lands on option N-1;
# this helper takes the option index and does the translation. These paths had
# no coverage, and the bug they now guard made plain clicks select nothing.

async def _click_option(pilot, index, shift=False):
    await pilot.click("#pick-list", offset=(3, index + 1), shift=shift)
    await _settle(pilot)


def test_a_plain_click_selects_one_item():
    """Regression: overriding `_on_click` and calling `super()` toggled twice,
    because Textual dispatches a handler for every class in the MRO — so a
    single click selected and immediately deselected."""
    async def scenario():
        app = _Harness(options=UBUNTU)
        async with app.run_test(size=(100, 32)) as pilot:
            await _settle(pilot)
            lst = app.screen.query_one("#pick-list", SelectionList)
            await _click_option(pilot, 4)
            assert len(lst.selected) == 1, "a single click must select exactly one item"

    _run(scenario)


def test_a_second_plain_click_unticks_it():
    async def scenario():
        app = _Harness(options=UBUNTU)
        async with app.run_test(size=(100, 32)) as pilot:
            await _settle(pilot)
            lst = app.screen.query_one("#pick-list", SelectionList)
            await _click_option(pilot, 4)
            await _click_option(pilot, 4)
            assert list(lst.selected) == []

    _run(scenario)


def test_shift_click_takes_the_range_from_the_last_click():
    async def scenario():
        app = _Harness(options=UBUNTU)
        async with app.run_test(size=(100, 32)) as pilot:
            await _settle(pilot)
            lst = app.screen.query_one("#pick-list", SelectionList)
            await _click_option(pilot, 4)          # anchor
            first = set(lst.selected)
            await _click_option(pilot, 8, shift=True)
            picked = set(lst.selected)

            assert first < picked, "shift+click must extend, not replace"
            assert len(picked) == 5, f"expected a 5-item span, got {sorted(picked)}"

    _run(scenario)


def test_shift_click_does_not_untick_the_clicked_item():
    """It must never fall through to SelectionList's own toggle as well."""
    async def scenario():
        app = _Harness(options=UBUNTU)
        async with app.run_test(size=(100, 32)) as pilot:
            await _settle(pilot)
            lst = app.screen.query_one("#pick-list", SelectionList)
            await _click_option(pilot, 2)
            await _click_option(pilot, 6, shift=True)
            values = [o.value for o in lst.options]
            assert values[6] in lst.selected, "the shift-clicked item must end up selected"

    _run(scenario)


def test_shift_click_leaves_selections_outside_the_range_alone():
    async def scenario():
        app = _Harness(options=UBUNTU, selected=["noble-updates"])
        async with app.run_test(size=(100, 32)) as pilot:
            await _settle(pilot)
            lst = app.screen.query_one("#pick-list", SelectionList)
            await _click_option(pilot, 1)
            await _click_option(pilot, 3, shift=True)
            assert "noble-updates" in lst.selected

    _run(scenario)


def test_pick_page_is_a_page():
    from mmw.tui.pick import PickPage
    from mmw.tui.ui.page import Page
    assert issubclass(PickPage, Page) and PickPage.SCROLLS is False
