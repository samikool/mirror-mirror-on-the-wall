"""Edit `[settings]` — the two values `mmw init` asks for once.

Before this screen they could only change by "Re-init", which deletes
`mmw.toml`: there was no route from the TUI to a different pool directory or
connection count that kept the configured repositories and networks.
"""
from rich.markup import escape as _escape
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static

from mmw.core import config
from mmw.tui.widgets import Button


class SettingsScreen(ModalScreen["config.Settings | None"]):
    """Dismisses with the edited `Settings`, or None on cancel."""

    DEFAULT_CSS = """
    SettingsScreen { align: center middle; }
    SettingsScreen > Vertical {
        width: 84;
        max-width: 96%;
        height: auto;
        max-height: 96%;
        border: double $primary;
        background: $surface;
    }
    SettingsScreen #settings-title {
        height: 1;
        background: $primary-darken-3;
        padding: 0 2;
        color: $text;
        text-style: bold;
    }
    SettingsScreen #settings-body { height: auto; padding: 1 2; }
    SettingsScreen .field-label { color: $text-muted; }
    SettingsScreen .field-hint { color: $text-muted; margin-bottom: 1; }
    SettingsScreen #settings-nav { height: auto; padding: 0 2 1 2; }
    SettingsScreen #settings-nav Button { margin-left: 1; }
    SettingsScreen #settings-status { width: 1fr; height: auto; padding: 1 0 0 0; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, settings: config.Settings):
        super().__init__()
        self._settings = settings

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("  Settings", id="settings-title")
            with Vertical(id="settings-body"):
                yield Label("Pool directory (mirror_dir)", classes="field-label")
                yield Input(str(self._settings.mirror_dir), id="settings-mirror-dir")
                yield Label(
                    "Where the pool, keys and certs live. Changing it does not move "
                    "anything — move the directory yourself, or the next sync starts "
                    "an empty pool there.", classes="field-hint")
                yield Label("Parallel downloads (max_connections)", classes="field-label")
                yield Input(str(self._settings.max_connections),
                            id="settings-max-connections", type="integer")
            with Horizontal(id="settings-nav"):
                yield Label("", id="settings-status")
                yield Button("Cancel", id="btn-settings-cancel", variant="default")
                yield Button("Save", id="btn-settings-save", variant="primary")

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-settings-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-settings-save")
    def _save(self) -> None:
        mirror_dir = self.query_one("#settings-mirror-dir", Input).value.strip()
        conns = self.query_one("#settings-max-connections", Input).value.strip()
        if not mirror_dir:
            return self._refuse("✗ the pool directory cannot be empty")
        try:
            n = int(conns)
        except ValueError:
            n = 0
        if n < 1:
            return self._refuse("✗ parallel downloads must be a whole number, 1 or more")
        self.dismiss(config.Settings.model_validate({
            **self._settings.model_dump(),
            "mirror_dir": mirror_dir,
            "max_connections": n,
        }))

    def _refuse(self, message: str) -> None:
        self.query_one("#settings-status", Label).update(f"[red]{_escape(message)}[/]")
        self.notify(message, severity="warning")
