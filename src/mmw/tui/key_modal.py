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

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.markup import escape
from textual.widgets import Input, Label, OptionList, TextArea

from mmw.core import keys
from mmw.core.draft import RepositoryDraft
from mmw.tui.path_pick import PathPickPage
from mmw.tui.ui.form import Heading
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


def apply_key(draft: RepositoryDraft, result) -> bool:
    """Put a `KeyPage` result on `draft`. False when it was cancelled. Every
    caller (adding, the bulk review, editing) goes through here, so a key
    fetched from a URL keeps that URL wherever it was chosen."""
    if result is None:
        return False
    armour, name, url = result
    draft.set_key(armour or None, name=name, url=url,
                  source="fetched" if url else "pasted" if armour else "")
    return True


def key_summary(draft: RepositoryDraft, url: str | None = None) -> str:
    """One line for a Key field: what the repository's key is, and where it
    comes from when that is a URL (`url`, else the draft's own)."""
    url = (url if url is not None else draft.gpg_key_url) or ""
    state = draft.key_state()
    if url:
        return (f"[$mmw-ok]key set[/] [dim]· also from {escape(url)}[/]" if state == "have"
                else f"[$mmw-ok]from {escape(url)}[/]")
    return {
        "have": "[$mmw-ok]key set[/]",
        "needs": f"[$mmw-warn]needs {escape(draft.gpg_key_name or '')}[/]",
        "none": "[dim]no key[/]",
    }[state]


class KeyPage(Page):
    """Supply or clear one repository's key.

    Dismisses with `(armour, name, url)` — `url` is where the key was fetched
    from, while the box still holds exactly that key, else None — `("", None,
    None)` to clear, or `None` when cancelled — the same "None means leave it
    alone" convention as `PickPage`.
    """

    def __init__(self, repo_name: str, key_name: str | None = None,
                 armour: str | None = None, key_dir: str = ""):
        super().__init__()
        self.TITLE = f"GPG key — {repo_name}"
        self.subtitle = f"needs {key_name}" if key_name else "no keyring named"
        self._repo_name = repo_name
        self._key_name = key_name
        self._armour = armour or ""
        self._key_dir = key_dir
        self._found: dict[str, Path] = {}
        self._names: list[str] = []
        # (url, armour) of the last fetch: the URL is handed back with the key
        # only while the box still holds exactly what it fetched.
        self._fetched: tuple[str, str] | None = None

    def compose_body(self) -> ComposeResult:
        yield Heading("Search a directory for keyrings")
        with Horizontal(id="dir-row"):
            yield Input(placeholder="/path/to/keys", id="key-dir", value=self._key_dir,
                        compact=True)
            yield Button("Browse…", id="btn-browse", compact=True)
            yield Button("Scan", id="btn-scan", variant="primary", compact=True)
        yield OptionList(id="key-found", compact=True)
        yield Heading("…or fetch it from a URL")
        with Horizontal(id="url-row"):
            yield Input(placeholder="https://example.com/repo.gpg", id="key-url", compact=True)
            yield Button("Fetch", id="btn-fetch", variant="primary", compact=True)
        yield Heading("…or paste the armoured key")
        yield TextArea(self._armour, id="key-area", show_line_numbers=False, compact=True)
        yield Label("", id="key-status")

    def compose_actions(self) -> ComposeResult:
        yield Button("Clear key", id="btn-clear", compact=True)
        yield Button("Cancel", id="btn-cancel", compact=True)
        yield Button("Save", id="btn-save", variant="primary", compact=True)

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
        picked = await self.app.push_screen_wait(PathPickPage(
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
            status.update("[dim]Enter a directory to search.[/]")
            return
        status.update("[dim]Searching…[/]")
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
            status.update(f"[$mmw-warn]No key files under {escape(path)}[/]")
            return
        # The wanted keyring first: with 24 keys in a DGX bundle, the one this
        # repository actually names should not need hunting for.
        names = sorted(found, key=lambda n: (n != self._key_name, n.lower()))
        ol.add_options([f"{'→ ' if n == self._key_name else '  '}{n}" for n in names])
        self._names = names
        ol.display = True
        if self._key_name in found:
            status.update(f"[$mmw-ok]✓ found {escape(self._key_name)}[/] — select it to load")
            ol.highlighted = 0
        else:
            status.update(f"[dim]{len(found)} key(s) found[/]")

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
            self.query_one("#key-status", Label).update(f"[$mmw-error]✗ {escape(str(e))}[/]")
            return
        self.query_one("#key-area", TextArea).text = armour
        self._key_name = name
        self.query_one("#key-status", Label).update(
            f"[$mmw-ok]✓ loaded {escape(name)}[/] [dim]({len(armour)} bytes armoured)[/]"
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
            status.update("[dim]Enter a URL to fetch.[/]")
            return
        status.update("[dim]Fetching…[/]")
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
        self.app.call_from_thread(self._on_fetched, url, armour)

    def _on_fetched(self, url: str, armour: str) -> None:
        self._fetched = (url, armour.strip())
        self.query_one("#key-area", TextArea).text = armour
        self.query_one("#key-status", Label).update(
            f"[$mmw-ok]✓ fetched[/] [dim]({len(armour)} bytes armoured)[/]"
        )

    def _on_fetch_failed(self, error: Exception) -> None:
        self.query_one("#key-status", Label).update(
            f"[$mmw-error]✗ {escape(str(error) or type(error).__name__)}[/]"
        )

    # ── result ───────────────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-clear")
    def _clear(self) -> None:
        self.dismiss(("", None, None))

    @on(Button.Pressed, "#btn-save")
    def _save(self) -> None:
        text = self.query_one("#key-area", TextArea).text.strip()
        if text and "BEGIN PGP PUBLIC KEY BLOCK" not in text:
            self.query_one("#key-status", Label).update(
                "[$mmw-error]✗ That is not an armoured public key block.[/]"
            )
            return
        url = self._fetched[0] if self._fetched and self._fetched[1] == text else None
        self.dismiss((text, self._key_name, url))
