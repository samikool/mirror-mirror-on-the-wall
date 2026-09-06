"""Reviewing a whole apt configuration at once.

`cat /etc/apt/sources.list.d/*` on a real machine — a DGX Spark ships about a
dozen — names many repositories in one paste. One set of pickers cannot express
that, and importing it wholesale gives the user no say over the terabytes it
implies. So the bundle gets a pane: one row per repository, each drilling into
the *same* `PickModal` and `KeyModal` the single-repository step uses.

Rows are `RepositoryDraft`s, so nothing here re-implements probing, the
"empty means all" rule, or the conversion to `SourceConfig`.
"""
from pathlib import Path

from rich.markup import escape as _escape
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Input, Label

from mmw.core import keys
from mmw.core.draft import DIMENSIONS, RepositoryDraft
from mmw.tui.key_modal import KeyModal
from mmw.tui.path_pick import PathPickModal
from mmw.tui.pick import PickModal
from mmw.tui.widgets import Button

_DIM_TITLES = {"suites": "suites", "components": "components", "arches": "architectures"}

# Widget IDs must be [a-zA-Z0-9_-]; a repository index is the only safe handle.
def _row_id(idx: int, part: str) -> str:
    return f"repo-{idx}-{part}"


class RepoRow(Vertical):
    """One repository: include-or-not, its three dimensions, and its key."""

    DEFAULT_CSS = """
    RepoRow {
        height: auto;
        border: round $primary-darken-2;
        margin: 0 0 1 0;
        padding: 0 1;
    }
    RepoRow.excluded { border: round $surface-lighten-2; }
    RepoRow .head { height: auto; }
    RepoRow Checkbox { border: none; padding: 0; margin: 0; width: auto; }
    RepoRow .repo-name { width: auto; color: $text; text-style: bold; padding: 0 1; }
    RepoRow .repo-tag { width: auto; padding: 0 1; }
    RepoRow .repo-url {
        width: 1fr;
        color: $text-muted;
        height: 1;
        padding: 0 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    RepoRow .dims { height: auto; }
    RepoRow .dims Button { min-width: 0; width: auto; margin-right: 1; }
    RepoRow .dim-text {
        width: 1fr;
        height: 1;
        padding: 1 0 0 1;
        color: $accent;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    def __init__(self, idx: int, draft: RepositoryDraft, existing: bool):
        super().__init__(id=_row_id(idx, "row"))
        self._idx = idx
        self.draft = draft
        self._existing = existing
        self.included = True

    def compose(self) -> ComposeResult:
        d = self.draft
        # "merge" is not cosmetic: `config.merge_sources()` keys on the
        # repository URI, so this row will widen an existing source rather than
        # create a second one — worth saying before the user hits Add.
        tag = ("[yellow]merge[/]" if self._existing else "[dim]new[/]")
        with Horizontal(classes="head"):
            yield Checkbox(value=True, id=_row_id(self._idx, "on"))
            yield Label(_escape(d.name), classes="repo-name", id=_row_id(self._idx, "name"))
            yield Label(tag, classes="repo-tag")
            yield Label(_escape(d.url), classes="repo-url")
        with Horizontal(classes="dims"):
            for dim in DIMENSIONS:
                yield Button(_DIM_TITLES[dim].capitalize() + "…",
                             id=_row_id(self._idx, f"btn-{dim}"), variant="default")
            yield Button("Key…", id=_row_id(self._idx, "btn-key"), variant="default")
        yield Label("", classes="dim-text", id=_row_id(self._idx, "summary"))

    def on_mount(self) -> None:
        self.refresh_summary()

    def refresh_summary(self) -> None:
        d = self.draft
        if d.error:
            text = f"[red]✗ {_escape(d.error)}[/]"
        else:
            text = "  ·  ".join(
                f"[dim]{_DIM_TITLES[dim]}[/] {_escape(d.summary(dim))}" for dim in DIMENSIONS
            )
        state = d.key_state()
        if state == "have":
            text += f"  ·  [green]✓ key[/] [dim]({d.key_source or 'inline'})[/]"
        elif state == "needs":
            text += f"  ·  [yellow]⚠ needs {_escape(d.gpg_key_name)}[/]"
        self.query_one(f"#{_row_id(self._idx, 'summary')}", Label).update(text)

    @on(Checkbox.Changed)
    def _toggled(self, event: Checkbox.Changed) -> None:
        event.stop()
        self.included = event.value
        self.set_class(not event.value, "excluded")


class BulkReviewScreen(ModalScreen):
    """Configure a bundle of repositories. Dismisses with the included drafts,
    or None when cancelled."""

    DEFAULT_CSS = """
    BulkReviewScreen { align: center middle; }
    BulkReviewScreen > Vertical {
        width: 120;
        max-width: 96%;
        height: 90%;
        max-height: 96%;
        border: double $primary;
        background: $surface;
    }
    BulkReviewScreen #bulk-title {
        height: 1;
        background: $primary-darken-3;
        padding: 0 2;
        color: $text;
        text-style: bold;
    }
    BulkReviewScreen #bulk-hint { height: 1; padding: 0 2; color: $text-muted; }
    BulkReviewScreen #keydir-row { height: 3; padding: 0 2; }
    BulkReviewScreen #keydir-row Input { width: 1fr; }
    BulkReviewScreen #keydir-row Button { width: auto; min-width: 8; margin-left: 1; }
    BulkReviewScreen #keydir-status { height: 1; padding: 0 2; color: $text-muted; }
    BulkReviewScreen #repo-list { height: 1fr; padding: 0 2; }
    BulkReviewScreen #bulk-bar {
        height: auto;
        align: right middle;
        padding: 0 2;
        background: $surface;
        border-top: solid $primary-darken-2;
        overflow-x: auto;
    }
    BulkReviewScreen #bulk-bar Button { margin-left: 1; min-width: 0; width: auto; }
    BulkReviewScreen #bulk-bar .spacer { width: 1fr; height: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, drafts: list[RepositoryDraft], existing: set[str] | None = None):
        super().__init__()
        self._drafts = drafts
        self._existing = existing or set()
        self._key_dir = ""

    def compose(self) -> ComposeResult:
        needs = sum(1 for d in self._drafts if d.key_state() == "needs")
        with Vertical():
            yield Label(f"  Review {len(self._drafts)} repositories", id="bulk-title")
            yield Label(
                "  untick what you don't want · each row drills into the same pickers",
                id="bulk-hint",
            )
            yield Label(
                f"  Key directory  [dim]({needs} repositor{'y' if needs == 1 else 'ies'} "
                "named a keyring we don't have)[/]" if needs else "  Key directory",
                id="keydir-label",
            )
            with Horizontal(id="keydir-row"):
                yield Input(placeholder="/path/to/keys — searched recursively", id="keydir")
                yield Button("Browse…", id="btn-keydir-browse", variant="default")
                yield Button("Scan", id="btn-keydir", variant="primary")
            yield Label("", id="keydir-status")
            with VerticalScroll(id="repo-list"):
                for i, d in enumerate(self._drafts):
                    yield RepoRow(i, d, d.identity in self._existing)
            with Horizontal(id="bulk-bar"):
                yield Button("Select all", id="btn-all", variant="default")
                yield Button("Clear", id="btn-none", variant="default")
                yield Label("", classes="spacer")
                yield Button("Cancel", id="btn-cancel", variant="default")
                yield Button("Done", id="btn-done", variant="primary")

    # ── the key directory, applied to every row at once ──────────────────────

    @on(Button.Pressed, "#btn-keydir")
    def _keydir_pressed(self, event: "Button.Pressed") -> None:
        event.stop()
        self._scan(self.query_one("#keydir", Input).value.strip())

    @on(Input.Submitted, "#keydir")
    def _keydir_submitted(self, event: "Input.Submitted") -> None:
        self._scan(event.value.strip())

    @on(Button.Pressed, "#btn-keydir-browse")
    def _keydir_browse(self, event: "Button.Pressed") -> None:
        event.stop()
        self._browse_keydir()

    @work
    async def _browse_keydir(self) -> None:
        picked = await self.app.push_screen_wait(PathPickModal(
            "Where are the keyrings?",
            "searched recursively, matched to each `Signed-By:` by basename",
            self.query_one("#keydir", Input).value.strip(),
        ))
        if picked is None:
            return                                  # cancelled: leave it alone
        self.query_one("#keydir", Input).value = picked
        self._scan(picked)

    def _scan(self, path: str) -> None:
        if not path:
            return
        self._key_dir = path
        self.query_one("#keydir-status", Label).update("  [dim]Searching…[/]")
        self._scan_worker(path)

    @work(thread=True, exit_on_error=False)
    def _scan_worker(self, path: str) -> None:
        found = keys.find_keys(Path(path).expanduser())
        # Reading is I/O too — binary keyrings get armoured here, off the UI
        # thread, so the main loop never blocks on a directory of 24 keys.
        loaded: dict[str, str] = {}
        for name, p in found.items():
            try:
                loaded[name] = keys.read_key(p)
            except Exception:
                continue
        self.app.call_from_thread(self._on_scanned, path, loaded)

    def _on_scanned(self, path: str, loaded: "dict[str, str]") -> None:
        status = self.query_one("#keydir-status", Label)
        if not loaded:
            status.update(f"  [yellow]No key files under {_escape(path)}[/]")
            return
        matched = 0
        for row in self.query(RepoRow):
            d = row.draft
            # Only fills a gap: a key the pasted config carried inline is the
            # authority and is never overwritten by a same-named file.
            if d.gpg_key or not d.gpg_key_name:
                continue
            armour = loaded.get(d.gpg_key_name)
            if armour is None:
                continue
            d.set_key(armour, source="dir")
            matched += 1
            row.refresh_summary()
        still = sum(1 for d in self._drafts if d.key_state() == "needs")
        tail = f"  [yellow]{still} still missing[/]" if still else "  [green]all resolved[/]"
        status.update(f"  [green]✓ {len(loaded)} key(s) found, {matched} matched[/]{tail}")

    # ── per-row drill-down ───────────────────────────────────────────────────

    @on(Button.Pressed)
    def _row_button(self, event: "Button.Pressed") -> None:
        bid = event.button.id or ""
        if not bid.startswith("repo-"):
            return
        event.stop()
        _, idx, part = bid.split("-", 2)
        row = self.query_one(f"#{_row_id(int(idx), 'row')}", RepoRow)
        if part == "btn-key":
            self._choose_key(row)
        else:
            self._choose_dimension(row, part.removeprefix("btn-"))

    @work
    async def _choose_dimension(self, row: RepoRow, dim: str) -> None:
        d = row.draft
        picked = await self.app.push_screen_wait(PickModal(
            f"Choose {_DIM_TITLES[dim]}", d.url, d.available[dim], d.chosen[dim],
        ))
        if picked is None:
            return                                  # cancelled: leave it alone
        d.chosen[dim] = picked
        row.refresh_summary()
        if dim == "suites" and picked:
            # Components and architectures are per-suite; a different suite may
            # publish a different set, so re-probe rather than keep stale offers.
            self._reprobe_worker(row, picked[0])

    @work(thread=True, exit_on_error=False)
    def _reprobe_worker(self, row: RepoRow, suite: str) -> None:
        row.draft.probe_suite(suite)
        self.app.call_from_thread(row.refresh_summary)

    @work
    async def _choose_key(self, row: RepoRow) -> None:
        d = row.draft
        result = await self.app.push_screen_wait(
            KeyModal(d.name, d.gpg_key_name, d.gpg_key, self._key_dir)
        )
        if result is None:
            return
        armour, name = result
        d.set_key(armour, name=name, source="pasted" if armour else "")
        row.refresh_summary()

    # ── result ───────────────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-all")
    def _all(self, event: "Button.Pressed") -> None:
        event.stop()
        self._set_all(True)

    @on(Button.Pressed, "#btn-none")
    def _none(self, event: "Button.Pressed") -> None:
        event.stop()
        self._set_all(False)

    def _set_all(self, value: bool) -> None:
        for cb in self.query("#repo-list Checkbox").results(Checkbox):
            cb.value = value

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self, event: "Button.Pressed") -> None:
        event.stop()
        self.dismiss(None)

    @on(Button.Pressed, "#btn-done")
    def _done(self, event: "Button.Pressed") -> None:
        event.stop()
        self.dismiss([r.draft for r in self.query(RepoRow) if r.included])
