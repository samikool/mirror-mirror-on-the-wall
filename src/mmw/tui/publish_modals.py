"""The Networks verbs' dialogs: delta-or-full for a bundle, and the certs page.

Publish / dry run / bundle progress is `tui/transfer.py` (the Publish section)."""
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.markup import escape
from textual.screen import ModalScreen
from textual.widgets import Label

from mmw.tui.ui.form import Heading
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


class BundleModeModal(ModalScreen["bool | None"]):
    """Delta or full, asked every time a bundle starts.

    Returns `True` for full, `False` for delta, `None` on cancel (Escape or
    the Cancel button) — three real outcomes, not two: declining this choice
    must abort the bundle entirely, not silently fall back to delta the way
    `ConfirmDialog`'s plain yes/no would if reused here.
    """


    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, network_name: str):
        super().__init__()
        self._network_name = network_name

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"Bundle — {escape(self._network_name)}", classes="title")
            yield Label(
                "Send only what changed since the last bundle, "
                "or the whole network again?"
            )
            with Horizontal():
                yield Button("Cancel", id="cancel", compact=True)
                yield Button("Delta", variant="primary", id="delta", compact=True)
                yield Button("Full", variant="warning", id="full", compact=True)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#delta")
    def _delta(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#full")
    def _full(self) -> None:
        self.dismiss(True)


class CertsPage(Page):
    """Generate a private key + CSR for a network's hostnames: a preview
    (Generate → True), or the result of generating (Close)."""

    def __init__(self, network_name: str, hostnames: list[str],
                 key_path: "Path | None" = None, cnf_path: "Path | None" = None,
                 csr_path: "Path | None" = None, md_path: "Path | None" = None,
                 error: str = ""):
        super().__init__()
        self.TITLE = f"TLS certificates — {network_name}"
        self._network_name = network_name
        self._hostnames = hostnames
        self._key_path = key_path
        self._cnf_path = cnf_path
        self._csr_path = csr_path
        self._md_path = md_path
        self._error = error

    def compose_body(self) -> ComposeResult:
        if self._error:
            yield Label(f"[$mmw-error]✗ {escape(self._error)}[/]", classes="error-line")
            return
        if self._key_path:
            yield Label("[$mmw-ok]✓ Files written[/]", classes="success-line")
            for label, path in (("Key", self._key_path), ("Cfg", self._cnf_path),
                                ("CSR", self._csr_path), ("Docs", self._md_path)):
                yield Label(f"[dim]{label + ':':<5}[/] [$mmw-path]{escape(str(path))}[/]",
                            classes="path-line")
            yield Heading(f"{len(self._hostnames)} hostnames covered")
        else:
            yield Heading(f"{len(self._hostnames)} hostnames to include")
        for h in self._hostnames:
            yield Label(escape(h), classes="host-entry")
        if self._key_path:
            yield Label(f"Send [b]{escape(self._csr_path.name)}[/] to your CA machine and "
                        "follow [b]CERTS.md[/].", classes="path-line")

    def compose_actions(self) -> ComposeResult:
        if self._key_path or self._error:
            yield Button("Close", variant="primary", id="btn-close", compact=True)
        else:
            yield Button("Cancel", id="btn-close", compact=True)
            yield Button("Generate", variant="primary", id="btn-generate", compact=True)

    @on(Button.Pressed, "#btn-close")
    def _close(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#btn-generate")
    def _generate(self) -> None:
        self.dismiss(True)
