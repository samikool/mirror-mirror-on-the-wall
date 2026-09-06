"""A focused multi-select modal for one repository dimension.

Suites are the dimension that needs room: archive.ubuntu.com publishes 55 of
them, and picking from that inline alongside four other fields is miserable. It
also needs range selection, which `SelectionList` does not provide.
"""
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, SelectionList
from textual.widgets.selection_list import Selection

from mmw.tui.widgets import Button, JumpSelectionList


class RangeSelectionList(JumpSelectionList):
    """SelectionList with anchor-to-target range selection.

    File-explorer selection, as far as a checkbox list allows: a plain click or
    space toggles one item (what ctrl+click does elsewhere — right for a
    multi-select list) and drops the anchor there; **shift+click** takes
    everything from that anchor to the item clicked. Keyboard equivalents:
    **shift+space** takes the range to wherever the cursor is, and shift+↑/↓
    extends it step by step.

    Ranges only ever *add*: selecting jammy…jammy-updates never disturbs
    anything already ticked outside that span.
    """

    BINDINGS = [
        Binding("shift+space", "extend_to_here", "Select range", show=False),
        Binding("shift+down", "extend_down", "Extend selection", show=False),
        Binding("shift+up", "extend_up", "Extend selection", show=False),
    ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._anchor: int | None = None

    def _option_values(self) -> list:
        # NOT `_values`: SelectionList already uses that name for its
        # value→index dict, and shadowing it breaks the base widget.
        return [o.value for o in self.options]

    def _select_range(self, start: int, end: int) -> None:
        """Tick everything between two indices, inclusive. Never unticks."""
        values = self._option_values()
        if not values:
            return
        lo, hi = sorted((start, end))
        lo = max(0, lo)
        hi = min(len(values) - 1, hi)
        for value in values[lo : hi + 1]:
            self.select(value)

    def action_extend_to_here(self) -> None:
        """shift+space — the keyboard twin of shift+click."""
        if self.highlighted is None:
            return
        if self._anchor is None:
            self._anchor = self.highlighted
        self._select_range(self._anchor, self.highlighted)

    def action_extend_down(self) -> None:
        self._step(1)

    def action_extend_up(self) -> None:
        self._step(-1)

    def _step(self, delta: int) -> None:
        values = self._option_values()
        if not values or self.highlighted is None:
            return
        if self._anchor is None:
            self._anchor = self.highlighted
        target = max(0, min(len(values) - 1, self.highlighted + delta))
        self.highlighted = target
        self._select_range(self._anchor, target)

    async def _on_click(self, event) -> None:
        """shift+click takes the range from the anchor to the clicked item.

        A plain click is left entirely alone so `SelectionList` toggles it as
        usual. **Never call `super()._on_click()` here**: Textual dispatches a
        handler for *every* class in the MRO (`_get_dispatch_methods`), so
        calling the base explicitly toggles twice and cancels itself out — which
        made plain clicks select nothing at all.
        """
        if not event.shift:
            return
        clicked = event.style.meta.get("option")
        if clicked is None or self.options[clicked].disabled:
            return
        if self._anchor is None:
            self._anchor = clicked
        self.highlighted = clicked
        self._select_range(self._anchor, clicked)
        # Stops the MRO walk, so SelectionList does not also toggle the item.
        event.prevent_default()
        event.stop()

    @on(SelectionList.SelectionToggled)
    def _remember_anchor(self, event) -> None:
        """A plain toggle (key or click) starts a new range from there."""
        self._anchor = event.selection_index


class PickModal(ModalScreen):
    """Pick any number of values from a list. Dismisses with the chosen list,
    or None when cancelled."""

    DEFAULT_CSS = """
    PickModal { align: center middle; }
    PickModal > Vertical {
        width: 78;
        max-width: 92%;
        height: 80%;
        max-height: 90%;
        border: double $primary;
        background: $surface;
    }
    PickModal #pick-title {
        height: 1;
        background: $primary-darken-3;
        padding: 0 2;
        color: $text;
        text-style: bold;
    }
    PickModal #pick-subtitle {
        height: 1;
        padding: 0 2;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    PickModal #pick-hint {
        height: 1;
        padding: 0 2;
        color: $text-muted;
        width: 1fr;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    PickModal RangeSelectionList { height: 1fr; margin: 1 2 0 2; }
    PickModal #pick-count {
        height: 1;
        padding: 0 2;
        color: $accent;
    }
    PickModal #pick-bar {
        height: auto;   /* +1 row for border-top; 3-row buttons need 3 content rows */
        align: right middle;
        padding: 0 2;
        background: $surface;
        border-top: solid $primary-darken-2;
        overflow-x: auto;
    }
    PickModal #pick-bar Button { margin-left: 1; min-width: 0; width: auto; }
    PickModal #pick-bar .spacer { width: 1fr; height: 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, title: str, subtitle: str, options: list[str],
                 selected: list[str] | None = None):
        super().__init__()
        self._title = title
        self._subtitle = subtitle
        self._options = options
        self._selected = set(selected or ())

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"  {self._title}", id="pick-title")
            yield Label(f"  {self._subtitle}", id="pick-subtitle")
            yield Label(
                "  click/space toggles · shift+click or shift+space takes the range",
                id="pick-hint",
            )
            yield RangeSelectionList(
                *[
                    Selection(o, o, o in self._selected)
                    for o in self._options
                ],
                id="pick-list",
            )
            yield Label("", id="pick-count")
            with Horizontal(id="pick-bar"):
                yield Button("Select all", id="btn-all", variant="default")
                yield Button("Clear", id="btn-none", variant="default")
                yield Label("", classes="spacer")
                yield Button("Cancel", id="btn-cancel", variant="default")
                yield Button("Done", id="btn-done", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#pick-list", SelectionList).focus()
        self._update_count()

    def _update_count(self) -> None:
        n = len(self.query_one("#pick-list", SelectionList).selected)
        total = len(self._options)
        self.query_one("#pick-count", Label).update(f"  {n} of {total} selected")

    @on(SelectionList.SelectedChanged, "#pick-list")
    def _changed(self) -> None:
        self._update_count()

    @on(Button.Pressed, "#btn-all")
    def _all(self) -> None:
        self.query_one("#pick-list", SelectionList).select_all()

    @on(Button.Pressed, "#btn-none")
    def _none(self) -> None:
        self.query_one("#pick-list", SelectionList).deselect_all()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-done")
    def _done(self) -> None:
        self.dismiss(list(self.query_one("#pick-list", SelectionList).selected))
