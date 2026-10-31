"""Signing keys — the keys mmw signs repositories with (`mmw signing-key`)."""
import asyncio
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.markup import escape
from textual.widgets import Input, Label, ListItem, ListView, TextArea

from mmw.core import disk_io
from mmw.core.signing import Keyring, SigningError
from mmw.tui.ui.dialog import ConfirmDialog
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


class SigningKeysPage(Page):
    TITLE = "Signing keys"
    HINT = ("Keys mmw signs your own repositories with. Generate one, or import an armoured "
            "private key. Export shows the public key clients install.")

    def __init__(self):
        super().__init__()
        self._key_rows = []

    def compose_body(self) -> ComposeResult:
        yield ListView(id="keys-list")
        yield Input(placeholder="name for a new or imported key, e.g. tools", id="key-name", compact=True)
        yield TextArea("", id="key-armor", read_only=True)

    def compose_actions(self) -> ComposeResult:
        yield Button("Generate", id="btn-key-generate", compact=True)
        yield Button("Import…", id="btn-key-import", compact=True)
        yield Button("Export public", id="btn-key-export", compact=True)
        yield Button("Delete", id="btn-key-delete", variant="warning", compact=True)

    def on_mount(self) -> None:
        # Its own worker, not a flow: a flow is one-at-a-time, and a Generate
        # pressed while the first listing finishes would be turned away.
        self.run_worker(self._reload(), group="keys-load", exit_on_error=False)

    def _keyring(self) -> Keyring:
        return Keyring(self.app.cfg.settings.mirror_dir)

    def _users(self, name: str) -> list[str]:
        return [s.name for s in self.app.cfg.sources if getattr(s, "key", None) == name]

    async def _reload(self) -> None:
        try:
            self._key_rows = await asyncio.to_thread(self._keyring().list)
        except SigningError as e:
            self.set_status(str(e), "error")
            return
        listing = self.query_one("#keys-list", ListView)
        await listing.clear()
        for k in self._key_rows:
            users = self._users(k.name)
            await listing.append(ListItem(Label(
                f"{escape(k.name)}  [dim]{k.fingerprint}  {k.created}[/]"
                + (f"  signs {escape(', '.join(users))}" if users else ""))))
        if not self._key_rows:
            self.set_status("No signing keys yet — type a name and Generate.")

    def _selected(self):
        index = self.query_one("#keys-list", ListView).index
        return self._key_rows[index] if index is not None and 0 <= index < len(self._key_rows) else None

    def _typed_name(self) -> str:
        return self.query_one("#key-name", Input).value.strip()

    @on(Button.Pressed, "#btn-key-generate")
    def _generate(self) -> None:
        self.start_flow(self._generate_flow)

    async def _generate_flow(self) -> None:
        try:
            key = await asyncio.to_thread(self._keyring().generate, self._typed_name())
        except SigningError as e:
            self.set_status(str(e), "error")
            return
        self.query_one("#key-name", Input).value = ""
        await self._reload()
        self.set_status(f"Generated {key.name}.", "ok")

    @on(Button.Pressed, "#btn-key-import")
    def _import(self) -> None:
        self.start_flow(self._import_flow)

    async def _import_flow(self) -> None:
        from mmw.tui.path_pick import PathPickPage
        picked = await self.app.push_screen_wait(PathPickPage(
            "Import a private key", "An armoured private key file (.asc, .key)", dirs_only=False))
        if picked is None:
            return
        name = self._typed_name() or Path(picked).stem
        try:
            text = await asyncio.to_thread(disk_io.ops.read_text, Path(picked))
            key = await asyncio.to_thread(self._keyring().import_armored, text, name)
        except (OSError, UnicodeDecodeError, SigningError) as e:
            self.set_status(str(e), "error")
            return
        await self._reload()
        self.set_status(f"Imported {key.name}.", "ok")

    @on(Button.Pressed, "#btn-key-export")
    def _export(self) -> None:
        self.start_flow(self._export_flow)

    async def _export_flow(self) -> None:
        key = self._selected()
        if key is None:
            self.set_status("Highlight a key to export.", "warning")
            return
        armor = await asyncio.to_thread(self._keyring().public_armor, key.name)
        self.query_one("#key-armor", TextArea).text = armor
        self.set_status(f"{key.name}'s public key — clients install it to trust what it signs.")

    @on(Button.Pressed, "#btn-key-delete")
    def _delete(self) -> None:
        self.start_flow(self._delete_flow)

    async def _delete_flow(self) -> None:
        key = self._selected()
        if key is None:
            self.set_status("Highlight a key to delete.", "warning")
            return
        users = self._users(key.name)
        if users:
            self.set_status(f"{key.name!r} signs {', '.join(users)} — change that source first.", "error")
            return
        if not await self.app.push_screen_wait(ConfirmDialog(
                "Delete signing key",
                f"Delete [b]{escape(key.name)}[/b]? Anything it signed can no longer be re-signed "
                "with it.", "Delete")):
            return
        await asyncio.to_thread(self._keyring().remove, key.name)
        await self._reload()
        self.set_status(f"Deleted {key.name}.", "ok")
