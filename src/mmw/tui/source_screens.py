"""Edit an already-configured repository.

The Networks tab has full CRUD; the Manage tab only ever had Add and Delete.
That is a real gap, not a cosmetic one: `config.merge_sources()` is
union-only by design (`_union_or_all()` returns `[]` when either side is
empty, deliberately, so importing one explicit arch can never *narrow* a
repository mirroring all of them — see design-notes.md). Re-running the add flow on
top of an existing source can therefore only ever widen it. Narrowing a
selection — dropping an arch, dropping a suite, restricting components — had
no route through the TUI at all; only delete-then-re-add or hand-editing
`mmw.toml` could do it. This screen is that missing route: it writes the
edited `SourceConfig` directly over the existing entry (`manage.py` replaces
by identity, never merges), so a narrower selection actually sticks.

Built on the same `RepositoryDraft` every other add-a-repository door uses
(`RepositoryDraft.from_source()` seeds one from an existing entry), so probing
and the "empty means all" rule are the same code as everywhere else — not a
second implementation that could drift from it.
"""
from rich.markup import escape as _escape
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static, Switch

from mmw.core import config
from mmw.core.draft import DIMENSIONS, RepositoryDraft
from mmw.tui.key_modal import KeyModal
from mmw.tui.pick import PickModal
from mmw.tui.widgets import Button

_DIM_TITLES = {"suites": "suites", "components": "components", "arches": "architectures"}


class EditSourceScreen(ModalScreen["config.SourceConfig | None"]):
    """Edit one configured repository's dimensions, key and flags.

    Dismisses with the edited `SourceConfig`, or `None` on cancel — the same
    "None means leave it alone" convention as every other modal here.
    """

    DEFAULT_CSS = """
    EditSourceScreen { align: center middle; }
    EditSourceScreen > Vertical {
        width: 84;
        max-width: 96%;
        height: 88%;
        max-height: 96%;
        border: double $primary;
        background: $surface;
    }
    EditSourceScreen #edit-source-title {
        height: 1;
        background: $primary-darken-3;
        padding: 0 2;
        color: $text;
        text-style: bold;
    }
    EditSourceScreen #edit-source-container { height: 1fr; overflow-y: auto; padding: 1 2; }
    EditSourceScreen .field-label { color: $text-muted; margin-bottom: 0; }
    EditSourceScreen #edit-source-url { color: $primary; margin-bottom: 1; }
    EditSourceScreen #edit-source-name { margin-bottom: 1; }
    EditSourceScreen .dim-row { height: auto; margin-bottom: 0; }
    EditSourceScreen .dim-row Button { min-width: 0; width: auto; }
    EditSourceScreen .dim-summary {
        /* `height: auto`, NOT `height: 1`. Textual boxes are border-box, so a
           declared height includes padding — `height: 1` with `padding-top: 1`
           left zero rows for content and the label rendered at height 0. It
           was present, displayed and visible, 58 columns wide, and drew
           nothing: opening this screen showed three "Choose…" buttons and no
           indication of what the repository currently mirrors, which is the
           one thing you open it to find out. */
        padding: 1 0 0 2;
        color: $accent;
        width: 1fr;
        height: auto;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    EditSourceScreen #probe-row { height: 3; align: left middle; }
    EditSourceScreen #probe-row Button { width: auto; min-width: 20; }
    EditSourceScreen #probe-status { padding: 0 0 0 2; color: $text-muted; }
    EditSourceScreen #sources-row, EditSourceScreen #du-row, EditSourceScreen #key-row {
        height: auto;   /* +1 row for border-top; 3-row buttons need 3 content rows */
        align: left middle;
        border-top: solid $primary-darken-2;
        margin-top: 1;
    }
    EditSourceScreen #sources-row Label, EditSourceScreen #du-row Label {
        margin: 0 0 0 1;
    }
    EditSourceScreen #key-row Button { width: auto; min-width: 12; }
    EditSourceScreen #key-status { padding: 0 0 0 2; color: $text-muted; }
    EditSourceScreen #nav-bar {
        height: auto;   /* +1 row for border-top; 3-row buttons need 3 content rows */
        align: right middle;
        padding: 0 2;
        background: $surface;
        border-top: solid $primary-darken-2;
        dock: bottom;
    }
    EditSourceScreen #nav-bar Button { margin-left: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, source: config.SourceConfig):
        super().__init__()
        self._source = source
        self._draft = RepositoryDraft.from_source(source)
        # `RepositoryDraft.to_source()` has no field for these — see its
        # docstring — so they are held here and stamped back onto the result
        # at save time. Dropping them would silently switch off dist-upgrade
        # support or a `gpg_key_url` fetch on every edit.
        self._dist_upgrade = source.dist_upgrade
        self._gpg_key_url = source.gpg_key_url
        # Whether this repository arrived already mirroring *all* suites.
        # `suites = []` is the documented "empty means all" state and is what
        # `mmw add` writes whenever the suites are left open, so it is an
        # ordinary configuration — not an error to be corrected on save. See
        # `_save`'s guard, which must not confuse it with the other empty.
        self._suites_started_empty = not source.suites
        # Same reasoning: `RepositoryDraft` has no per-suite override concept
        # at all (the picker is one flat "components" list, not one per
        # suite), so it is carried through unconditionally rather than
        # dropped on every edit of a source that has one configured.
        self._suite_overrides = source.suite_overrides

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"  Editing  [bold]{self._source.name}[/]", id="edit-source-title")
            with Vertical(id="edit-source-container"):
                yield Label("Repository URL", classes="field-label")
                # Not an Input: the URL *is* identity (SourceConfig.identity
                # is normalize_url(url)) — changing it here would silently
                # orphan every refs/mirrors row filed under the old identity.
                # Delete-and-re-add is the honest route to a different URL.
                yield Label(self._source.url, id="edit-source-url")
                yield Label("Name", classes="field-label")
                yield Input(self._source.name, id="edit-source-name")
                for dim in DIMENSIONS:
                    with Horizontal(id=f"{dim}-row", classes="dim-row"):
                        yield Button(f"Choose {_DIM_TITLES[dim]}…", id=f"btn-{dim}",
                                    variant="primary")
                        yield Label("", id=f"{dim}-summary", classes="dim-summary")
                with Horizontal(id="probe-row"):
                    yield Button("Probe repository", id="btn-probe", variant="default")
                    yield Label("", id="probe-status")
                with Horizontal(id="sources-row"):
                    yield Switch(value=self._source.include_sources, id="include-sources-switch")
                    yield Label("Also mirror deb-src (source packages)")
                with Horizontal(id="du-row"):
                    yield Switch(value=self._source.dist_upgrade, id="du-switch")
                    yield Label("Enable Ubuntu dist-upgrade support")
                with Horizontal(id="key-row"):
                    yield Button("Key…", id="btn-key", variant="default")
                    yield Label("", id="key-status")
            with Horizontal(id="nav-bar"):
                yield Button("Cancel", id="btn-cancel-source", variant="default")
                yield Button("Save", id="btn-save-source", variant="primary")

    def on_mount(self) -> None:
        self._refresh_summaries()
        self._refresh_key_status()

    # ── dimension summaries ──────────────────────────────────────────────────

    def _refresh_summaries(self) -> None:
        for dim in DIMENSIONS:
            self.query_one(f"#{dim}-summary", Label).update(self._draft.summary(dim))

    @work
    async def _choose_dimension(self, dim: str) -> None:
        picked = await self.app.push_screen_wait(PickModal(
            f"Choose {_DIM_TITLES[dim]}", self._draft.url,
            self._draft.available[dim], self._draft.chosen[dim],
        ))
        if picked is None:
            return                                  # cancelled: leave it alone
        self._draft.chosen[dim] = picked
        self._refresh_summaries()

    @on(Button.Pressed, "#btn-suites")
    def _pick_suites(self, event: "Button.Pressed") -> None:
        event.stop()
        self._choose_dimension("suites")

    @on(Button.Pressed, "#btn-components")
    def _pick_components(self, event: "Button.Pressed") -> None:
        event.stop()
        self._choose_dimension("components")

    @on(Button.Pressed, "#btn-arches")
    def _pick_arches(self, event: "Button.Pressed") -> None:
        event.stop()
        self._choose_dimension("arches")

    # ── probing ──────────────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-probe")
    def _probe_pressed(self, event: "Button.Pressed") -> None:
        event.stop()
        btn = self.query_one("#btn-probe", Button)
        btn.disabled = True
        self.query_one("#probe-status", Label).update("[dim]Probing…[/]")
        self._probe_worker()

    @work(thread=True, exit_on_error=False)
    def _probe_worker(self) -> None:
        # Widens what is *offered* without moving what is already ticked —
        # the same rule every other probe entry point follows.
        self._draft.probe()
        self.app.call_from_thread(self._on_probed)

    def _on_probed(self) -> None:
        self.query_one("#btn-probe", Button).disabled = False
        self._refresh_summaries()
        status = self.query_one("#probe-status", Label)
        if self._draft.error:
            status.update(f"[red]✗ {_escape(self._draft.error)}[/]")
        else:
            status.update("[green]✓ probed[/]")

    # ── key ──────────────────────────────────────────────────────────────────

    def _refresh_key_status(self) -> None:
        state = self._draft.key_state()
        text = {
            "have": "[green]key set[/]",
            "needs": f"[yellow]needs {_escape(self._draft.gpg_key_name or '')}[/]",
            "none": "[dim]no key[/]",
        }[state]
        self.query_one("#key-status", Label).update(text)

    @on(Button.Pressed, "#btn-key")
    def _key_pressed(self, event: "Button.Pressed") -> None:
        event.stop()
        self._open_key()

    @work
    async def _open_key(self) -> None:
        result = await self.app.push_screen_wait(KeyModal(
            self._draft.name, self._draft.gpg_key_name, self._draft.gpg_key,
        ))
        if result is None:
            return                                  # cancelled: leave it alone
        armour, name = result
        self._draft.set_key(armour or None, name=name, source="pasted" if armour else "")
        self._refresh_key_status()

    # ── result ───────────────────────────────────────────────────────────────

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-cancel-source")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-save-source")
    def _save(self) -> None:
        # An empty suites list has two different meanings here and only one of
        # them is a mistake, so this guard cannot simply test for emptiness the
        # way `StepCustom.get_sources()` does.
        #
        # A repository that arrived with `suites = []` is the documented
        # "empty means all" state — `resolve_source()` fills it in by listing
        # `dists/` at sync time, and `mmw add` writes exactly this whenever the
        # suites are left open. Refusing it would mean a repository mirroring
        # every suite could not be opened and saved at all: Save would silently
        # do nothing, with the modal just sitting there.
        #
        # Clearing a repository that *did* state its suites down to zero is the
        # real mistake, and that is what StepCustom's guard is for — the add
        # flow only ever sees freshly-probed drafts, so there emptiness can only
        # mean "the user unticked everything".
        if (self._draft.provider == "apt"
                and not self._draft.chosen["suites"]
                and not self._suites_started_empty):
            self.notify(
                "Choose at least one suite, or cancel to keep this repository "
                "mirroring the suites it already has.",
                severity="warning",
            )
            return
        name = self.query_one("#edit-source-name", Input).value.strip()
        self._draft.name = name or self._draft.name
        self._draft.include_sources = self.query_one("#include-sources-switch", Switch).value
        source = self._draft.to_source()
        source.dist_upgrade = self.query_one("#du-switch", Switch).value
        source.gpg_key_url = self._gpg_key_url
        source.suite_overrides = self._suite_overrides
        self.dismiss(source)
