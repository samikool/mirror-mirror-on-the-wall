"""Reviewing a whole apt configuration at once.

`cat /etc/apt/sources.list.d/*` on a real machine — a DGX Spark ships about a
dozen — names many repositories in one paste. One set of pickers cannot express
that, and importing it wholesale gives the user no say over the terabytes it
implies. So the bundle gets a pane: one row per repository, each drilling into
the *same* `PickPage` and `KeyPage` the single-repository step uses.

Rows are `RepositoryDraft`s, so nothing here re-implements probing, the
"empty means all" rule, or the conversion to `SourceConfig`.
"""
from pathlib import Path

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.markup import escape
from textual.widgets import Checkbox, Input, Label

from mmw.core import keys
from mmw.core.draft import DIMENSIONS, RepositoryDraft
from mmw.tui.key_modal import KeyPage, apply_key
from mmw.tui.path_pick import PathPickPage
from mmw.tui.pick import PickPage
from mmw.tui.ui.form import Heading
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button

_DIM_TITLES = {"suites": "suites", "components": "components", "arches": "architectures"}

# Widget IDs must be [a-zA-Z0-9_-]; a repository index is the only safe handle.
def _row_id(idx: int, part: str) -> str:
    return f"repo-{idx}-{part}"


class RepoRow(Vertical):
    """One repository: include-or-not, its three dimensions, and its key."""


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
        tag = ("[$mmw-warn]merge[/]" if self._existing else "[dim]new[/]")
        with Horizontal(classes="head"):
            yield Checkbox(value=True, id=_row_id(self._idx, "on"), compact=True)
            yield Label(escape(d.name), classes="repo-name", id=_row_id(self._idx, "name"))
            yield Label(tag, classes="repo-tag")
            yield Label(escape(d.url), classes="repo-url")
        with Horizontal(classes="dims"):
            for dim in DIMENSIONS:
                yield Button(_DIM_TITLES[dim].capitalize() + "…",
                             id=_row_id(self._idx, f"btn-{dim}"), compact=True)
            yield Button("Key…", id=_row_id(self._idx, "btn-key"), compact=True)
        yield Label("", classes="dim-text", id=_row_id(self._idx, "summary"))

    def on_mount(self) -> None:
        self.refresh_summary()

    def _dimension_text(self) -> str:
        """The three dimensions, positionally, in the order of the buttons above.

        Deliberately drops the dimension names. The three buttons directly
        above this line are labelled Suites… / Components… / Architectures…,
        so repeating those words here spent the row's width restating what is
        already on screen one line up — the same duplication the manage list
        had when it printed a full URL under its own host heading. Measured:
        the named form ran to 108 columns for a repository with everything
        open, against roughly 64 available at an 80-column terminal, so it
        truncated in the *common* case rather than an edge one.

        **All three slots are always rendered, even when a dimension is open.**
        Positional labelling only works if the positions hold still; dropping
        the open ones would silently shift `arm64` from third place to second
        and make the reader count buttons to decode it.
        """
        d = self.draft
        if all(not d.stored(dim) for dim in DIMENSIONS):
            # Everything open. Saying it once beats saying it three times, and
            # this is the ordinary state of a freshly pasted repository.
            return "[dim]all — resolved at sync[/]"
        parts = []
        for dim in DIMENSIONS:
            stored = d.stored(dim)
            if not stored:
                parts.append("[dim]all[/]")
            elif len(stored) <= 3:
                parts.append(escape(", ".join(stored)))
            else:
                parts.append(escape(f"{', '.join(stored[:2])} +{len(stored) - 2}"))
        return "  ·  ".join(parts)

    def refresh_summary(self) -> None:
        d = self.draft
        if d.error:
            text = f"[$mmw-error]✗ {escape(d.error)}[/]"
        else:
            text = self._dimension_text()
        state = d.key_state()
        if state == "have":
            text += f"  ·  [$mmw-ok]✓ key[/] [dim]({d.key_source or 'inline'})[/]"
        elif state == "needs":
            text += f"  ·  [$mmw-warn]⚠ needs {escape(d.gpg_key_name)}[/]"
        self.query_one(f"#{_row_id(self._idx, 'summary')}", Label).update(text)

    @on(Checkbox.Changed)
    def _toggled(self, event: Checkbox.Changed) -> None:
        event.stop()
        self.included = event.value
        self.set_class(not event.value, "excluded")


class BulkReviewPage(Page):
    """Configure a bundle of repositories. Dismisses with the included drafts,
    or None when cancelled (Esc or Cancel)."""

    SCROLLS = False

    def __init__(self, drafts: list[RepositoryDraft], existing: set[str] | None = None):
        super().__init__()
        self.TITLE = f"Review {len(drafts)} repositories"
        self.subtitle = "untick what you don't want · each row drills into the same pickers"
        self._drafts = drafts
        self._existing = existing or set()
        self._key_dir = ""

    def compose_body(self) -> ComposeResult:
        needs = sum(1 for d in self._drafts if d.key_state() == "needs")
        yield Heading(
            f"Key directory ({needs} repositor{'y' if needs == 1 else 'ies'} named a keyring "
            "we don't have)" if needs else "Key directory")
        with Horizontal(id="keydir-row"):
            yield Input(placeholder="/path/to/keys — searched recursively", id="keydir",
                        compact=True)
            yield Button("Browse…", id="btn-keydir-browse", compact=True)
            yield Button("Scan", id="btn-keydir", variant="primary", compact=True)
        yield Label("", id="keydir-status")
        with VerticalScroll(id="repo-list"):
            for i, d in enumerate(self._drafts):
                yield RepoRow(i, d, d.identity in self._existing)

    def compose_actions(self) -> ComposeResult:
        yield Button("Select all", id="btn-all", compact=True)
        yield Button("Clear", id="btn-none", compact=True)
        yield Button("Cancel", id="btn-cancel", compact=True)
        yield Button("Done", id="btn-done", variant="primary", compact=True)

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
        picked = await self.app.push_screen_wait(PathPickPage(
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
        self.query_one("#keydir-status", Label).update("[dim]Searching…[/]")
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
            status.update(f"[$mmw-warn]No key files under {escape(path)}[/]")
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
        tail = f"  [$mmw-warn]{still} still missing[/]" if still else "  [$mmw-ok]all resolved[/]"
        status.update(f"[$mmw-ok]✓ {len(loaded)} key(s) found, {matched} matched[/]{tail}")

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
        picked = await self.app.push_screen_wait(PickPage(
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
            KeyPage(d.name, d.gpg_key_name, d.gpg_key, self._key_dir)
        )
        if apply_key(d, result):
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

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self, event: "Button.Pressed") -> None:
        event.stop()
        self.dismiss(None)

    @on(Button.Pressed, "#btn-done")
    def _done(self, event: "Button.Pressed") -> None:
        event.stop()
        self.dismiss([r.draft for r in self.query(RepoRow) if r.included])
