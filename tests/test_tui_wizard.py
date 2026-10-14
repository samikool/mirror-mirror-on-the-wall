"""Wizard: a page with steps, Back/Next, and one result."""
import asyncio

from textual.app import ComposeResult
from textual.widgets import Label

from mmw.tui.ui.base import ThemedApp
from mmw.tui.ui.wizard import Wizard
from waiting import settle, until


class _Two(Wizard):
    TITLE = "Two"
    STEPS = ("First", "Second")

    def __init__(self):
        super().__init__()
        self.ok = True
        self.entered = []

    def compose_body(self) -> ComposeResult:
        yield from super().compose_body()
        yield Label("one", id="s0")
        yield Label("two", id="s1")

    async def enter(self, step):
        self.entered.append(step)
        self.query_one("#s0").display = step == 0
        self.query_one("#s1").display = step == 1

    async def leave(self, step):
        return self.ok

    def result(self):
        return "finished"


def _drive(actions):
    async def go():
        got = []

        class _Host(ThemedApp):
            def on_mount(self):
                self.push_screen(_Two(), got.append)

        app = _Host()
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            await actions(app, pilot)
            await settle(pilot)
        return got
    return asyncio.run(go())


def _press(app, which):
    app.screen.query_one(f"#btn-wizard-{which}").press()


def test_next_back_and_finish():
    async def act(app, pilot):
        page = app.screen
        assert "First" in str(page.query_one("#wizard-steps").render())
        assert page.query_one("#btn-wizard-back").disabled
        _press(app, "next")
        await pilot.pause()
        assert page.step == 1 and str(page.query_one("#btn-wizard-next").label) == "Done ✓"
        _press(app, "back")
        await pilot.pause()
        assert page.step == 0 and page.entered == [0, 1, 0]
        _press(app, "next")
        await pilot.pause()
        _press(app, "next")
    assert _drive(act) == ["finished"]


def test_leave_false_stays_and_cancel_returns_none():
    async def act(app, pilot):
        app.screen.ok = False
        _press(app, "next")
        await pilot.pause()
        assert app.screen.step == 0
        _press(app, "cancel")
    assert _drive(act) == [None]


def test_controls_are_one_row():
    async def act(app, pilot):
        assert all(b.region.height == 1 for b in app.screen.query("Button"))
        assert app.screen.query_one("#wizard-steps").region.height == 1
    _drive(act)


def test_a_double_press_of_next_does_not_skip_a_step():
    """A double-click or key-repeat used to land the second press on the next
    step's button — skipping Confirm and adding straight away."""
    async def act(app, pilot):
        btn = app.screen.query_one("#btn-wizard-next")
        btn.press()
        btn.press()
        await until(pilot, lambda: app.screen.step >= 1)
        await settle(pilot)
        assert app.screen.step == 1
        _press(app, "next")                      # a deliberate later press still works
    assert _drive(act) == ["finished"]
