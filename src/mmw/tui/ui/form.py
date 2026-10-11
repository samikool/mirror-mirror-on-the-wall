"""Forms: a label · control row per field, an optional hint under it.

Every field is one row tall (compact controls), labels sit in one column, and
long text truncates. A field's hint is not drawn beside it: the enclosing Page
shows the focused field's hint on its one-row hint line. `Form.values()` is keyed by each control's id — the same
id `tests/test_tui_covers_config.py` looks for — so a page reads exactly what
the config-coverage test says it edits.
"""
from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widget import Widget
from textual.widgets import Checkbox, Input, Label, Select, Static, TextArea

from mmw.tui.ui.widgets import Button


class Heading(Static):
    """A one-row label that starts a group of fields."""

    def __init__(self, text: str):
        super().__init__(text, classes="form-heading", markup=False)


class Field(Vertical):
    """One labelled control on one row. Subclasses provide `control()` and
    `value`. The hint is shown by the enclosing Page while the field has focus."""

    HAS_VALUE = True

    def __init__(self, label: str, *, id: str, hint: str = "", field_id: str | None = None):
        # `field_id` lets a converted screen keep the row id its logic already
        # toggles (StepCustom shows/hides `#{dim}-row`).
        super().__init__(id=field_id or f"field-{id}")
        self.label = label
        self.control_id = id
        self.hint = hint

    def compose(self) -> ComposeResult:
        with Horizontal(classes="field-row"):
            yield Label(self.label, classes="field-label", markup=False)
            yield self.control()

    def control(self) -> Widget:
        raise NotImplementedError

    @property
    def value(self) -> object:
        raise NotImplementedError

    def validate(self) -> str | None:
        """A message saying what is wrong, or None."""
        return None


class TextField(Field):
    def __init__(self, label: str, value: str = "", *, id: str, hint: str = "",
                 required: bool = False, integer_min: int | None = None,
                 placeholder: str = "", field_id: str | None = None):
        super().__init__(label, id=id, hint=hint, field_id=field_id)
        self._initial = value
        self._required = required
        self._integer_min = integer_min
        self._placeholder = placeholder

    def control(self) -> Widget:
        return Input(
            self._initial, id=self.control_id, compact=True,
            placeholder=self._placeholder, classes="field-control",
            type="integer" if self._integer_min is not None else "text",
        )

    @property
    def raw(self) -> str:
        return self.query_one(f"#{self.control_id}", Input).value.strip()

    @property
    def value(self) -> str | int:
        return int(self.raw) if self._integer_min is not None else self.raw

    def validate(self) -> str | None:
        raw = self.raw
        if self._integer_min is not None:
            try:
                ok = int(raw) >= self._integer_min
            except ValueError:
                ok = False
            if not ok:
                return f"{self.label} must be a whole number, {self._integer_min} or more"
        elif self._required and not raw:
            return f"{self.label} cannot be empty"
        return None


class ChoiceField(Field):
    def __init__(self, label: str, options: list[tuple[str, str]], value: str, *,
                 id: str, hint: str = "", field_id: str | None = None):
        super().__init__(label, id=id, hint=hint, field_id=field_id)
        self._options = options
        self._initial = value

    def control(self) -> Widget:
        return Select(self._options, value=self._initial, allow_blank=False,
                      compact=True, id=self.control_id, classes="field-control")

    @property
    def value(self) -> str:
        return self.query_one(f"#{self.control_id}", Select).value


class Form(Vertical):
    def __init__(self, *fields: Field, id: str | None = None):
        super().__init__(*fields, id=id)

    @property
    def fields(self) -> list[Field]:
        return list(self.query(Field))

    def errors(self) -> list[str]:
        return [m for f in self.fields if (m := f.validate())]

    def values(self) -> dict[str, object]:
        return {f.control_id: f.value for f in self.fields if f.HAS_VALUE}


class ToggleField(Field):
    def __init__(self, label: str, value: bool, *, id: str, hint: str = "",
                 field_id: str | None = None):
        super().__init__(label, id=id, hint=hint, field_id=field_id)
        self._initial = value

    def control(self) -> Widget:
        return Checkbox("", self._initial, id=self.control_id, compact=True,
                        classes="field-control")

    @property
    def value(self) -> bool:
        return self.query_one(f"#{self.control_id}", Checkbox).value


class TextAreaField(Field):
    """Multi-line text; four rows tall (mmw.tcss `.field-textarea`)."""

    def __init__(self, label: str, text: str, *, id: str, hint: str = "",
                 field_id: str | None = None):
        super().__init__(label, id=id, hint=hint, field_id=field_id)
        self._initial = text

    def control(self) -> Widget:
        return TextArea(self._initial, id=self.control_id, compact=True,
                        show_line_numbers=False, classes="field-control field-textarea")

    @property
    def value(self) -> str:
        return self.query_one(f"#{self.control_id}", TextArea).text

    def lines(self) -> list[str]:
        """Non-blank lines, stripped, first occurrence kept."""
        return list(dict.fromkeys(ln.strip() for ln in self.value.splitlines() if ln.strip()))


class ActionField(Field):
    """A label, what is currently chosen, and a button that changes it."""

    HAS_VALUE = False

    def __init__(self, label: str, summary: str, button_label: str, *, id: str,
                 summary_id: str, hint: str = "", field_id: str | None = None):
        super().__init__(label, id=id, hint=hint, field_id=field_id)
        self._summary = summary
        self._button_label = button_label
        self._summary_id = summary_id

    def compose(self) -> ComposeResult:
        with Horizontal(classes="field-row"):
            yield Label(self.label, classes="field-label", markup=False)
            yield Label(self._summary, id=self._summary_id, classes="field-summary")
            yield Button(self._button_label, id=self.control_id, compact=True)

    def set_summary(self, text: str) -> None:
        """Markup allowed — callers escape any user text in it."""
        self.query_one(f"#{self._summary_id}", Label).update(text)


class PathField(TextField):
    """A path typed, or browsed for with PathPickPage."""

    def __init__(self, label: str, value: str = "", *, id: str, hint: str = "",
                 required: bool = False, browse_title: str = "Choose a directory",
                 browse_subtitle: str = "", browse_id: str | None = None,
                 dirs_only: bool = True, suffixes: tuple[str, ...] = (),
                 field_id: str | None = None):
        super().__init__(label, value, id=id, hint=hint, required=required, field_id=field_id)
        self._browse_title = browse_title
        self._browse_subtitle = browse_subtitle
        self.browse_id = browse_id or f"{id}-browse"
        self._dirs_only = dirs_only
        self._suffixes = suffixes

    def compose(self) -> ComposeResult:
        with Horizontal(classes="field-row"):
            yield Label(self.label, classes="field-label", markup=False)
            yield self.control()
            yield Button("Browse…", id=self.browse_id, compact=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == self.browse_id:
            event.stop()
            self.run_worker(self._browse(), exclusive=True)

    async def _browse(self) -> None:
        from mmw.tui.path_pick import PathPickPage
        picked = await self.app.push_screen_wait(PathPickPage(
            self._browse_title, self._browse_subtitle, start=self.raw,
            dirs_only=self._dirs_only, suffixes=self._suffixes))
        if picked is not None:
            # Assigning posts Input.Changed, so anything watching the field
            # (a preview line) updates as if it had been typed.
            self.query_one(f"#{self.control_id}", Input).value = picked
