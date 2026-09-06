"""Providing a GPG key for a repository by hand.

Airgapped is the whole point: a `gpg_key_url` cannot be fetched from the
network that needs the key, and a `Signed-By: /usr/share/keyrings/foo.gpg`
names a file that lives on the machine the config came from, not in the config.
So the key has to arrive some other way, and there are exactly two:

1. **Paste it** — armoured text straight into the box.
2. **Point at a directory** — the keyrings copied off the source machine.
   `keys.find_keys()` walks it recursively and matches by basename, which is
   what a `Signed-By:` path gives us. Binary keyrings are accepted: `read_key`
   armours them losslessly (`enarmor`), and `write_key` emits both encodings
   again at sync time.
"""
from pathlib import Path

from rich.markup import escape as _escape
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList, TextArea

from mmw.core import keys
from mmw.tui.path_pick import PathPickModal
from mmw.tui.widgets import Button


class KeyModal(ModalScreen):
    """Supply or clear one repository's key.

    Dismisses with `(armour, name)`, `("", None)` to clear, or `None` when
    cancelled — the same "None means leave it alone" convention as `PickModal`.
    """

    DEFAULT_CSS = """
    KeyModal { align: center middle; }
    KeyModal > Vertical {
        width: 92;
        max-width: 96%;
        height: 84%;
        max-height: 92%;
        border: double $primary;
        background: $surface;
    }
    KeyModal #key-title {
        height: 1;
        background: $primary-darken-3;
        padding: 0 2;
        color: $text;
        text-style: bold;
    }
    KeyModal #key-subtitle {
        height: 1;
        padding: 0 2;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    KeyModal .section { height: 1; padding: 0 2; color: $accent; margin-top: 1; }
    KeyModal #dir-row { height: 3; padding: 0 2; }
    KeyModal #dir-row Input { width: 1fr; }
    KeyModal #dir-row Button { width: auto; min-width: 8; margin-left: 1; }
    KeyModal #key-found { height: auto; max-height: 8; margin: 0 2; border: solid $primary-darken-2; }
    KeyModal #key-area { height: 1fr; margin: 0 2; }
    KeyModal #key-status { height: 1; padding: 0 2; color: $text-muted; }
    KeyModal #key-bar {
        height: auto;
        align: right middle;
        padding: 0 2;
        background: $surface;
        border-top: solid $primary-darken-2;
        overflow-x: auto;
    }
    KeyModal #key-bar Button { margin-left: 1; min-width: 0; width: auto; }
    KeyModal #key-bar .spacer { width: 1fr; height: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, repo_name: str, key_name: str | None = None,
                 armour: str | None = None, key_dir: str = ""):
        super().__init__()
        self._repo_name = repo_name
        self._key_name = key_name
        self._armour = armour or ""
        self._key_dir = key_dir
        self._found: dict[str, Path] = {}
        self._names: list[str] = []

    def compose(self) -> ComposeResult:
        wanted = f"needs [b]{_escape(self._key_name)}[/]" if self._key_name else "no keyring named"
        with Vertical():
            yield Label(f"  GPG key — {_escape(self._repo_name)}", id="key-title")
            yield Label(f"  {wanted}", id="key-subtitle")
            yield Label("  Search a directory for keyrings", classes="section")
            with Horizontal(id="dir-row"):
                yield Input(placeholder="/path/to/keys", id="key-dir", value=self._key_dir)
                yield Button("Browse…", id="btn-browse", variant="default")
                yield Button("Scan", id="btn-scan", variant="primary")
            yield OptionList(id="key-found")
            yield Label("  …or paste the armoured key", classes="section")
            yield TextArea(self._armour, id="key-area", show_line_numbers=False)
            yield Label("", id="key-status")
            with Horizontal(id="key-bar"):
                yield Button("Clear key", id="btn-clear", variant="default")
                yield Label("", classes="spacer")
                yield Button("Cancel", id="btn-cancel", variant="default")
                yield Button("Save", id="btn-save", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#key-found").display = False
        if self._key_dir:
            self._scan(self._key_dir)
        else:
            self.query_one("#key-dir", Input).focus()

    # ── scanning a directory ─────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-scan")
    def _scan_pressed(self, event: "Button.Pressed") -> None:
        event.stop()
        self._scan(self.query_one("#key-dir", Input).value.strip())

    @on(Input.Submitted, "#key-dir")
    def _scan_submitted(self, event: "Input.Submitted") -> None:
        self._scan(event.value.strip())

    @on(Button.Pressed, "#btn-browse")
    def _browse_pressed(self, event: "Button.Pressed") -> None:
        event.stop()
        self._browse()

    @work
    async def _browse(self) -> None:
        picked = await self.app.push_screen_wait(PathPickModal(
            "Where are the keyrings?",
            "searched recursively for .gpg / .asc / .key / .pub",
            self.query_one("#key-dir", Input).value.strip(),
        ))
        if picked is None:
            return                                  # cancelled: leave it alone
        self.query_one("#key-dir", Input).value = picked
        self._scan(picked)

    def _scan(self, path: str) -> None:
        status = self.query_one("#key-status", Label)
        if not path:
            status.update("  [dim]Enter a directory to search.[/]")
            return
        status.update("  [dim]Searching…[/]")
        self._scan_worker(path)

    @work(thread=True, exit_on_error=False)
    def _scan_worker(self, path: str) -> None:
        found = keys.find_keys(Path(path).expanduser())
        self.app.call_from_thread(self._on_scanned, path, found)

    def _on_scanned(self, path: str, found: "dict[str, Path]") -> None:
        self._found = found
        status = self.query_one("#key-status", Label)
        ol = self.query_one("#key-found", OptionList)
        ol.clear_options()
        if not found:
            ol.display = False
            status.update(f"  [yellow]No key files under {_escape(path)}[/]")
            return
        # The wanted keyring first: with 24 keys in a DGX bundle, the one this
        # repository actually names should not need hunting for.
        names = sorted(found, key=lambda n: (n != self._key_name, n.lower()))
        ol.add_options([f"{'→ ' if n == self._key_name else '  '}{n}" for n in names])
        self._names = names
        ol.display = True
        if self._key_name in found:
            status.update(f"  [green]✓ found {_escape(self._key_name)}[/] — select it to load")
            ol.highlighted = 0
        else:
            status.update(f"  [dim]{len(found)} key(s) found[/]")

    @on(OptionList.OptionSelected, "#key-found")
    def _load_selected(self, event) -> None:
        name = self._names[event.option_index]
        try:
            armour = keys.read_key(self._found[name])
        except Exception as e:
            self.query_one("#key-status", Label).update(f"  [red]✗ {_escape(str(e))}[/]")
            return
        self.query_one("#key-area", TextArea).text = armour
        self._key_name = name
        self.query_one("#key-status", Label).update(
            f"  [green]✓ loaded {_escape(name)}[/] [dim]({len(armour)} bytes armoured)[/]"
        )

    # ── result ───────────────────────────────────────────────────────────────

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-clear")
    def _clear(self) -> None:
        self.dismiss(("", None))

    @on(Button.Pressed, "#btn-save")
    def _save(self) -> None:
        text = self.query_one("#key-area", TextArea).text.strip()
        if text and "BEGIN PGP PUBLIC KEY BLOCK" not in text:
            self.query_one("#key-status", Label).update(
                "  [red]✗ That is not an armoured public key block.[/]"
            )
            return
        self.dismiss((text, self._key_name))
