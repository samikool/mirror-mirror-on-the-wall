import re

from textual.binding import Binding
from textual.widgets import Button as _Button, SelectionList


class Button(_Button):
    """Button that activates on both Enter and Space."""
    BINDINGS = [*_Button.BINDINGS, Binding("space", "press", show=False)]


class JumpSelectionList(SelectionList):
    """SelectionList that jumps to matching items when a letter is typed.
    Press the same letter again to cycle through matches.

    Ctrl+A toggles the whole list — a repository with 55 suites is unusable
    without it.
    """

    BINDINGS = [
        Binding("ctrl+a", "toggle_all", "Select/deselect all", show=False),
    ]

    def action_toggle_all(self) -> None:
        enabled = [o for o in self.options if not o.disabled]
        if len(self.selected) >= len(enabled):
            self.deselect_all()
        else:
            self.select_all()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._jump_char: str = ""
        self._jump_idx: int = -1

    @staticmethod
    def _plain(prompt) -> str:
        return re.sub(r"\[.*?\]", "", str(prompt)).strip().lower()

    def on_key(self, event) -> None:
        ch = event.character or ""
        if len(ch) != 1 or not ch.isalpha():
            return
        char = ch.lower()
        options = list(self.options)
        if not options:
            return

        start = (self._jump_idx + 1) % len(options) if char == self._jump_char and self._jump_idx >= 0 else 0

        for i in range(len(options)):
            idx = (start + i) % len(options)
            opt = options[idx]
            if not opt.disabled and self._plain(opt.prompt).startswith(char):
                self.highlighted = idx
                self._jump_char = char
                self._jump_idx = idx
                event.stop()
                return

        self._jump_char = ""
        self._jump_idx = -1
