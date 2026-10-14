"""Validated provider form state and its shared, metadata-driven controls."""
import copy

from pydantic import TypeAdapter, ValidationError
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Checkbox, Input, TextArea

from mmw.core import config
from mmw.providers import get_provider
from mmw.tui.pick import PickPage
from mmw.tui.ui.form import ActionField, Form, TextAreaField, TextField, ToggleField
from mmw.tui.ui.widgets import Button


def parse_top_n(text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    if not text.isascii() or not text.isdecimal() or int(text) < 1:
        raise ValueError("Top N must be a positive whole number or blank.")
    return int(text)


def parse_ids(text: str) -> list[str]:
    return list(dict.fromkeys(line.split("#", 1)[0].strip().lower()
                             for line in text.splitlines() if line.split("#", 1)[0].strip()))


class ProviderFormDraft:
    def __init__(self, source):
        self.data = copy.deepcopy(source.model_dump())

    def set_value(self, path, value):
        if path == "extensions.ids" and isinstance(value, str):
            value = parse_ids(value)
        node = self.data
        keys = path.split(".")
        for key in keys[:-1]:
            node = node[key]
        node[keys[-1]] = copy.deepcopy(value)

    def to_source(self):
        return TypeAdapter(config.AnySource).validate_python(copy.deepcopy(self.data))


class ProviderSourceForm(Vertical):
    def __init__(self, source, *, existing_names=None):
        super().__init__()
        self.source = source
        self.draft = ProviderFormDraft(source)
        self.existing_names = set(existing_names or ()) - {source.name}
        self.fields = get_provider(source.provider).fields

    @staticmethod
    def control_id(path):
        return "vs-" + path.removeprefix("extensions.").replace("_", "-")

    def compose(self) -> ComposeResult:
        with Form():
            for field in self.fields:
                cid = self.control_id(field.path)
                value = self.draft.data
                for key in field.path.split("."):
                    value = value[key]
                if field.kind == "multi":
                    yield ActionField(field.label, ", ".join(value) or "all", "Choose…", id=cid,
                                      summary_id=cid + "-summary", hint=field.hint)
                elif field.kind == "toggle":
                    yield ToggleField(field.label, value, id=cid, hint=field.hint)
                elif field.kind == "textarea":
                    yield TextAreaField(field.label, "\n".join(value), id=cid, hint=field.hint)
                else:
                    yield TextField(field.label, "" if value is None else str(value), id=cid, hint=field.hint)

    def focus_first(self):
        self.query_one("#vs-name", Input).focus()

    @on(Button.Pressed)
    def _pick_pressed(self, event):
        field = next((f for f in self.fields if f.kind == "multi"
                      and event.button.id == self.control_id(f.path)), None)
        if field is not None:
            event.stop()
            self.run_worker(self._pick(field), group="provider-pick", exclusive=True)

    async def _pick(self, field):
        cid = self.control_id(field.path)
        chosen = await self.app.push_screen_wait(PickPage(
            field.label, "Empty means all; Cancel keeps your previous choice.",
            list(field.choices), self.draft.data[field.path]))
        if chosen is not None:
            self.draft.set_value(field.path, chosen)
            self.query_one("#field-" + cid, ActionField).set_summary(", ".join(chosen) or "all")
            self.post_message(self.Changed())
        self.query_one("#" + cid).focus()

    from textual.message import Message

    class Changed(Message):
        pass

    @on(Input.Changed)
    @on(TextArea.Changed)
    @on(Checkbox.Changed)
    def _changed(self):
        self.post_message(self.Changed())

    def get_source(self, *, focus_errors=True):
        for field in self.fields:
            if field.kind == "multi":
                continue
            widget = self.query_one("#" + self.control_id(field.path))
            if field.kind == "toggle":
                value = widget.value
            elif field.kind == "textarea":
                value = widget.text
            else:
                value = widget.value.strip()
                if field.kind == "optional-positive-int":
                    try:
                        value = parse_top_n(value)
                    except ValueError:
                        if focus_errors:
                            widget.focus()
                            widget.scroll_visible()
                        raise
            self.draft.set_value(field.path, value)
        try:
            source = self.draft.to_source()
        except ValidationError as exc:
            location = [str(part) for part in exc.errors()[0]['loc'] if isinstance(part, str)]
            if location and location[0] == self.source.provider:
                location.pop(0)
            path = '.'.join(location)
            if path == 'extensions':
                path = 'extensions.ids'  # ExtensionSelection's model validator checks IDs.
            field = next((f for f in self.fields if path == f.path or path.startswith(f.path + '.')), None)
            if field is not None and focus_errors:
                widget = self.query_one('#' + self.control_id(field.path))
                widget.focus()
                widget.scroll_visible()
            raise
        if not source.name or source.name in self.existing_names:
            if focus_errors:
                self.focus_first()
            raise ValueError("Choose a nonempty, unique source name.")
        return source
