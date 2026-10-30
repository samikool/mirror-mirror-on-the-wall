"""A folder repository's client sources file, to read, copy or save."""
import asyncio
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.widgets import Select, TextArea

from mmw.core import disk_io
from mmw.providers.apt_local import sources_file
from mmw.tui.ui.form import ChoiceField, Form
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button

_FORMATS = [("deb822 (.sources) — Ubuntu 24.04+, Debian 12+", "deb822"),
            ("one line (.list) — Ubuntu 22.04, Debian 11 and older", "list")]


class SourcesFilePage(Page):
    TITLE = "Sources file"

    def __init__(self, source):
        super().__init__()
        self.source = source
        self.subtitle = source.url

    def compose_body(self) -> ComposeResult:
        yield Form(ChoiceField("Format", _FORMATS, "deb822", id="sources-format",
                               hint="What the clients run: apt's newer deb822 or the one-line form."))
        yield TextArea(sources_file(self.source, "deb822"), id="sources-text", read_only=True)

    def compose_actions(self) -> ComposeResult:
        yield Button("Save…", id="btn-sources-save", variant="primary", compact=True)

    def _fmt(self) -> str:
        return self.query_one("#sources-format", Select).value

    @on(Select.Changed, "#sources-format")
    def _format_changed(self) -> None:
        self.query_one("#sources-text", TextArea).text = sources_file(self.source, self._fmt())

    @on(Button.Pressed, "#btn-sources-save")
    def _save(self) -> None:
        self.start_flow(self._save_flow)

    async def _save_flow(self) -> None:
        from mmw.tui.path_pick import PathPickPage
        picked = await self.app.push_screen_wait(PathPickPage(
            "Save the sources file", "The directory to write it in", dirs_only=True))
        if picked is None:
            return
        ext = "sources" if self._fmt() == "deb822" else "list"
        dest = Path(picked) / f"{self.source.name}.{ext}"
        try:
            await asyncio.to_thread(disk_io.ops.write_text, dest, self.query_one("#sources-text", TextArea).text)
        except OSError as e:
            self.set_status(f"Not saved: {e}", "error")
            return
        self.set_status(f"Saved {dest}.", "ok")
