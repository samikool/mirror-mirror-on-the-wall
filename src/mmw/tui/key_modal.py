"""Providing a GPG key for a repository by hand.

Airgapped is the whole point: `keys.sync_custom_key()`'s `gpg_key_url` fetch
happens on the network that *mirrors*, not the one adding the repository, and
a `Signed-By: /usr/share/keyrings/foo.gpg` names a file that lives on the
machine the config came from, not in the config. So the key has to arrive some
other way, and there are three:

1. **Point at a directory** — the keyrings copied off the source machine.
   `keys.find_keys()` walks it recursively and matches by basename, which is
   what a `Signed-By:` path gives us. Binary keyrings are accepted: `read_key`
   armours them losslessly (`enarmor`).
2. **Fetch it from a URL** — the vendor's own published key (this machine
   *can* usually reach the public internet even when it can't reach the
   airgapped side). `keys.fetch_key()` accepts either encoding, same as
   `read_key()` for a local file: armoured, or the raw binary form vendor
   docs often tell you to produce yourself with `gpg --dearmor` — either way
   it lands in the box already armoured.
3. **Paste it** — armoured text straight into the box.

All three converge on the same box (`#key-area`) and the same validation, so
`write_key` emitting both `.asc` and `.gpg` encodings at sync time works
identically regardless of which door the key came in.
"""
import asyncio
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
        /* Scrolls, so the min-height floor on #key-area below can push content
           past the bottom on a short terminal without any of it becoming
           unreachable. */
        overflow-y: auto;
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
    KeyModal #url-row { height: 3; padding: 0 2; }
    KeyModal #url-row Input { width: 1fr; }
    KeyModal #url-row Button { width: auto; min-width: 8; margin-left: 1; }
    /* A fixed height, deliberately not `1fr`. This modal stacks ~18 rows of
       fixed furniture — title, subtitle, three section labels, a 3-row
       directory row, a 3-row URL row, a status line and the button bar —
       so in an 84%-height modal on an 80x24 terminal (the POSIX default)
       `1fr` resolved to **zero**: the paste box, the whole point of the
       screen, rendered at no height at all and the modal looked like it had
       simply lost its input. `min-height` does not rescue it either, because
       Textual clamps a flexible child to the space actually available. A
       fixed height plus the scrollable body above is what makes it
       deterministic: 8 usable rows at every terminal size, with anything
       that no longer fits reachable by scrolling. It does not grow on a tall
       terminal, which is the right trade — you paste a key here, you do not
       read one. */
    KeyModal #key-area { height: 10; margin: 0 2; }
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
            yield Label("  …or fetch it from a URL", classes="section")
            with Horizontal(id="url-row"):
                yield Input(placeholder="https://example.com/repo.gpg", id="key-url")
                yield Button("Fetch", id="btn-fetch", variant="primary")
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
    async def _load_selected(self, event) -> None:
        # `bulk_review._scan_worker` already reads keys on a thread ("Reading is
        # I/O too — binary keyrings get armoured here") and this did it inline.
        # One key is small, so it never looked like a freeze — until the
        # directory is on a stale NFS mount, which is exactly where an
        # airgapped operator's keyring copy tends to live.
        name = self._names[event.option_index]
        try:
            armour = await asyncio.to_thread(keys.read_key, self._found[name])
        except Exception as e:
            self.query_one("#key-status", Label).update(f"  [red]✗ {_escape(str(e))}[/]")
            return
        self.query_one("#key-area", TextArea).text = armour
        self._key_name = name
        self.query_one("#key-status", Label).update(
            f"  [green]✓ loaded {_escape(name)}[/] [dim]({len(armour)} bytes armoured)[/]"
        )

    # ── fetching from a URL ──────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-fetch")
    def _fetch_pressed(self, event: "Button.Pressed") -> None:
        event.stop()
        self._fetch(self.query_one("#key-url", Input).value.strip())

    @on(Input.Submitted, "#key-url")
    def _fetch_submitted(self, event: "Input.Submitted") -> None:
        self._fetch(event.value.strip())

    def _fetch(self, url: str) -> None:
        status = self.query_one("#key-status", Label)
        if not url:
            status.update("  [dim]Enter a URL to fetch.[/]")
            return
        status.update("  [dim]Fetching…[/]")
        self._fetch_worker(url)

    @work(thread=True, exit_on_error=False)
    def _fetch_worker(self, url: str) -> None:
        import httpx
        try:
            with httpx.Client() as client:
                armour = keys.fetch_key(url, client)
        except Exception as e:
            self.app.call_from_thread(self._on_fetch_failed, e)
            return
        self.app.call_from_thread(self._on_fetched, armour)

    def _on_fetched(self, armour: str) -> None:
        self.query_one("#key-area", TextArea).text = armour
        self.query_one("#key-status", Label).update(
            f"  [green]✓ fetched[/] [dim]({len(armour)} bytes armoured)[/]"
        )

    def _on_fetch_failed(self, error: Exception) -> None:
        self.query_one("#key-status", Label).update(
            f"  [red]✗ {_escape(str(error) or type(error).__name__)}[/]"
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
