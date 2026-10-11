"""The form kit: one row per control, validation, values keyed by control id."""
import asyncio

from textual.app import ComposeResult
from textual.widgets import Input, Select

from mmw.tui.ui.base import ThemedApp
from mmw.tui.ui.form import ChoiceField, Form, TextField


def _form():
    return Form(
        TextField("Pool directory", "/srv/pool", id="f-dir", required=True,
                  hint="where things live"),
        TextField("Parallel downloads", "8", id="f-conns", integer_min=1),
        ChoiceField("Theme", [("mmw", "mmw"), ("nord", "nord")], "mmw", id="f-theme"),
        id="form",
    )


def _run(check, size=(100, 30)):
    class _App(ThemedApp):
        def compose(self) -> ComposeResult:
            yield _form()

    async def go():
        app = _App()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await check(app, pilot)
    asyncio.run(go())


def test_values_are_keyed_by_control_id_and_typed():
    async def check(app, pilot):
        form = app.query_one(Form)
        assert form.errors() == []
        assert form.values() == {"f-dir": "/srv/pool", "f-conns": 8, "f-theme": "mmw"}
    _run(check)


def test_every_control_is_one_row():
    async def check(app, pilot):
        for w in app.query("Input, Select"):
            assert w.region.height == 1, w
    _run(check)


def test_a_field_is_one_row_with_no_gap():
    async def check(app, pilot):
        fields = list(app.query(".field-row"))
        assert [f.region.height for f in fields] == [1, 1, 1]
        ys = [f.region.y for f in fields]
        assert ys == [ys[0], ys[0] + 1, ys[0] + 2]
        assert app.query_one("#field-f-dir").hint == "where things live"
        assert not app.query(".field-hint")
    _run(check)


def test_heading_is_one_row():
    from mmw.tui.ui.form import Heading

    class _App(ThemedApp):
        def compose(self) -> ComposeResult:
            yield Form(Heading("Where"), TextField("A", id="f-a"))

    async def go():
        app = _App()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app.query_one(Heading).region.height == 1
    asyncio.run(go())


def test_required_and_integer_rules():
    async def check(app, pilot):
        app.query_one("#f-dir", Input).value = "   "
        app.query_one("#f-conns", Input).value = "0"
        await pilot.pause()
        assert app.query_one(Form).errors() == [
            "Pool directory cannot be empty",
            "Parallel downloads must be a whole number, 1 or more",
        ]
    _run(check)


def test_choice_reflects_selection():
    async def check(app, pilot):
        app.query_one("#f-theme", Select).value = "nord"
        await pilot.pause()
        assert app.query_one(Form).values()["f-theme"] == "nord"
    _run(check)


def test_long_labels_truncate_instead_of_wrapping():
    class _App(ThemedApp):
        def compose(self) -> ComposeResult:
            yield Form(TextField("A label far too long to fit in its column " * 3, id="f-long"))

    async def go():
        app = _App()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app.query_one("#field-f-long .field-label").region.height == 1
    asyncio.run(go())


def test_toggle_textarea_and_action_fields():
    from textual.widgets import Checkbox, TextArea

    from mmw.tui.ui.form import ActionField, TextAreaField, ToggleField

    class _App(ThemedApp):
        def compose(self) -> ComposeResult:
            yield Form(
                ToggleField("Mirror deb-src", True, id="f-src"),
                TextAreaField("Extra files", "a\n\n b \na\n", id="f-extra"),
                ActionField("Suites", "all", "Choose…", id="btn-suites", summary_id="suites-summary"),
            )

    async def go():
        app = _App()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            form = app.query_one(Form)
            assert form.values() == {"f-src": True, "f-extra": "a\n\n b \na\n"}
            assert app.query_one("#field-f-extra").lines() == ["a", "b"]
            assert app.query_one("#f-src", Checkbox).region.height == 1
            assert app.query_one("#f-extra", TextArea).region.height == 4
            app.query_one("#field-btn-suites").set_summary("bookworm, trixie")
            await pilot.pause()
            assert str(app.query_one("#suites-summary").render()) == "bookworm, trixie"
            assert app.query_one("#btn-suites").region.height == 1
    asyncio.run(go())


def test_field_id_override():
    from mmw.tui.ui.form import ActionField

    class _App(ThemedApp):
        def compose(self) -> ComposeResult:
            yield Form(ActionField("Suites", "all", "Choose…", id="btn-suites",
                                   summary_id="suites-summary", field_id="suites-row"))

    async def go():
        app = _App()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert app.query_one("#suites-row").control_id == "btn-suites"
    asyncio.run(go())
