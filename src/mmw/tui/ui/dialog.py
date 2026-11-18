"""ConfirmDialog — the only modal besides help. Everything else is a page."""
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Label

from mmw.tui.ui.hit_test import LiveHitTest
from mmw.tui.ui.widgets import Button


class ConfirmDialog(LiveHitTest, ModalScreen[bool]):
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, title: str, message: str, confirm_label: str = "Confirm",
                 cancel_label: str = "Cancel", confirm_variant: str = "error",
                 cancel_variant: str = "primary", details: str = ""):
        super().__init__()
        self._title = title
        self._message = message
        self._confirm_label = confirm_label
        self._cancel_label = cancel_label
        self._confirm_variant = confirm_variant
        self._cancel_variant = cancel_variant
        self._details = details           # markup; scrolls when long

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(self._title, id="confirm-title", markup=False)
            if self._details:
                with VerticalScroll(id="confirm-details"):
                    yield Label(self._details)
            yield Label(self._message, id="confirm-message")
            with Horizontal(id="confirm-bar"):
                yield Button(self._cancel_label, variant=self._cancel_variant, id="cancel",
                             compact=True)
                yield Button(self._confirm_label, variant=self._confirm_variant, id="confirm",
                             compact=True)

    def action_cancel(self) -> None:
        """Escape declines — it must never trigger the destructive branch."""
        self.dismiss(False)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#confirm")
    def _confirm(self) -> None:
        self.dismiss(True)


class HelpDialog(LiveHitTest, ModalScreen[None]):
    """The keys of the page in front of you, plus that page's notes.

    The keys are handed in from the live bindings (`MmwApp.action_help`), so
    the list can never name a key that does nothing on this page."""

    BINDINGS = [
        Binding("escape", "close", "Close", show=False),
        Binding("question_mark", "close", "Close", show=False),
    ]

    def __init__(self, title: str, keys: list[tuple[str, str]], notes: str = ""):
        super().__init__()
        self._title = title
        self._keys = keys
        self._notes = notes

    def compose(self) -> ComposeResult:
        width = max((len(k) for k, _ in self._keys), default=1)
        with Vertical(id="help-box"):
            yield Label(f"{self._title} — keys", id="help-title", markup=False)
            with VerticalScroll(id="help-body"):
                for key, what in self._keys:
                    yield Label(f"[b $accent]{escape(key):<{width}}[/]  {escape(what)}",
                                classes="help-key")
                if self._notes:
                    yield Label(self._notes, id="help-notes")
            with Horizontal(id="help-bar"):
                yield Button("Close", id="btn-help-close", variant="primary", compact=True)

    def action_close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-help-close")
    def _close(self) -> None:
        self.dismiss(None)


class MoreMenu(LiveHitTest, ModalScreen["str | None"]):
    """A page's extra actions — the ones its action bar has no room for —
    each with its key. Picking one hands its action name back to the page.

    Nothing in the TUI is reachable only by a hidden key: what doesn't fit
    in the bar is listed here."""

    BINDINGS = [Binding("escape", "close", "Close", show=False)]

    def __init__(self, title: str, entries: list[tuple[str, str, str, bool]]):
        """`entries`: (action, label, key, available)."""
        super().__init__()
        self._title = title
        self._entries = entries

    def compose(self) -> ComposeResult:
        from textual.widgets import OptionList
        from textual.widgets.option_list import Option
        width = max((len(label) for _a, label, _k, _ok in self._entries), default=1)
        with Vertical(id="more-box"):
            yield Label(f"{self._title} — more", id="more-title", markup=False)
            yield OptionList(*(Option(f"{escape(label):<{width}}  [dim]{escape(key)}[/]", id=action,
                                      disabled=not ok)
                               for action, label, key, ok in self._entries),
                             id="more-list", compact=True)
            with Horizontal(id="more-bar"):
                yield Button("Close", id="btn-more-close", compact=True)

    def on_mount(self) -> None:
        self.query_one("#more-list").focus()

    def action_close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-more-close")
    def _close(self) -> None:
        self.dismiss(None)

    def on_option_list_option_selected(self, event) -> None:
        self.dismiss(event.option.id)
