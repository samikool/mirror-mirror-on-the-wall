"""Editable review of a recovered config, before Setup writes anything."""
from textual import on
from textual.app import ComposeResult
from textual.widgets import Label, TextArea

from mmw.commands.init_from_bundle import parse
from mmw.core import config
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


class BundleConfigReviewPage(Page):
    TITLE = "Review recovered configuration"

    def __init__(self, cfg: config.Config):
        super().__init__()
        self._cfg = cfg

    def compose_body(self) -> ComposeResult:
        yield Label("Edit sources, network settings or global settings. Nothing is written until Setup finishes.",
                    markup=False)
        yield TextArea(config.dumps(self._cfg), language="toml", id="recovered-config")

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-recovery-review-cancel", compact=True)
        yield Button("Use these settings", id="btn-recovery-review-save", variant="primary", compact=True)

    @on(Button.Pressed, "#btn-recovery-review-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-recovery-review-save")
    def _save(self) -> None:
        try:
            cfg = parse(self.query_one("#recovered-config", TextArea).text)
        except ValueError as exc:
            self.set_status(str(exc), "error")
            return
        self.dismiss(cfg)
