"""Edit an already-configured repository.

The Networks tab has full CRUD; the Manage tab only ever had Add and Delete.
That is a real gap, not a cosmetic one: `config.merge_sources()` is
union-only by design (`_union_or_all()` returns `[]` when either side is
empty, deliberately, so importing one explicit arch can never *narrow* a
repository mirroring all of them). Re-running the add flow on
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
from textual.widgets import Input, Label, Select, Static, Switch, TextArea

from mmw.core import config
from mmw.core.draft import DIMENSIONS, RepositoryDraft
from mmw.tui.key_modal import KeyModal
from mmw.tui.pick import PickModal
from mmw.tui.widgets import Button

_DIM_TITLES = {"suites": "suites", "components": "components", "arches": "architectures"}


def format_overrides(overrides: "list[config.SuiteOverride]") -> str:
    """`[[sources.suite_overrides]]` as the editor shows it: one
    `suite: comp, comp` line per override."""
    return "".join(f"{o.suite}: {', '.join(o.components)}\n" for o in overrides)


def parse_overrides(text: str) -> "list[config.SuiteOverride]":
    """The inverse of `format_overrides`. Raises ValueError naming the line."""
    out: list[config.SuiteOverride] = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        suite, sep, comps = line.partition(":")
        components = [c.strip() for c in comps.replace(" ", ",").split(",") if c.strip()]
        if not sep or not suite.strip() or not components:
            raise ValueError(f"suite override line {n} should read `suite: comp, comp`")
        out.append(config.SuiteOverride(suite=suite.strip(), components=components))
    return out


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
    EditSourceScreen #edit-source-url { margin-bottom: 1; }
    EditSourceScreen #edit-source-name { margin-bottom: 1; }
    EditSourceScreen #edit-source-label { margin-bottom: 1; }
    EditSourceScreen #edit-source-provider { margin-bottom: 1; width: 40; }
    EditSourceScreen #edit-source-key-url { margin-bottom: 1; }
    EditSourceScreen #edit-source-overrides { height: 5; margin-bottom: 1; }
    EditSourceScreen #edit-source-extra-files { height: 5; margin-bottom: 1; }
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
    EditSourceScreen #save-status {
        width: 1fr;
        height: auto;
        padding: 1 0 0 0;
        color: $error;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, source: config.SourceConfig,
                 existing_names: "set[str] | None" = None,
                 existing_urls: "set[str] | None" = None):
        super().__init__()
        self._source = source
        # The other repositories' identities: changing this one's URL onto
        # one of theirs would write two entries for one repository, which
        # the loader refuses — the same lock-out `existing_names` prevents.
        self._existing_urls = set(existing_urls or ())
        self._existing_urls.discard(source.identity)
        self._draft = RepositoryDraft.from_source(source)
        # The names of the *other* configured repositories, the same way
        # `AddMirrorScreen(existing_urls=…)` is handed what is already there.
        # Without it this screen could write a `mmw.toml` the loader then
        # refuses: `Config._names_must_be_unique` only runs when a `Config` is
        # *constructed*, and `manage.py` assigns `self._cfg.sources = [...]`
        # with `validate_assignment` off, so a duplicate name sails past both
        # the assignment and `config.save()` and only surfaces as a
        # `ValidationError` on the *next* load — locking the user out of every
        # command, with their inline GPG armour inside the file they now have
        # to hand-edit.
        self._existing_names = set(existing_names or ())
        self._existing_names.discard(source.name)
        # `RepositoryDraft.to_source()` has no field for these — see its
        # docstring — so they are held here and stamped back onto the result
        # at save time. Dropping them would silently switch off dist-upgrade
        # support or a `gpg_key_url` fetch on every edit.
        self._dist_upgrade = source.dist_upgrade
        self._gpg_key_url = source.gpg_key_url
        # `label` belongs in exactly the same category and was missed. It is
        # the GPG key grouping directory (`keys/{label}/`), so letting
        # `SourceConfig._fill_derived` re-derive it on a no-op save silently
        # relocates every key on the next sync and changes the published
        # `keys/` tree under the airgapped side.
        self._label = source.label
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
        # A stale probe must not clobber a selection the user has moved past —
        # the same generation-token rule `custom_repo.py` already followed and
        # every other worker-bearing screen did not.
        self._probe_gen = 0

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"  Editing  [bold]{self._source.name}[/]", id="edit-source-title")
            with Vertical(id="edit-source-container"):
                yield Label("Repository URL", classes="field-label")
                # Editable. The URL is identity, so a different one is a
                # different repository: the old one's pool content becomes
                # orphaned and `mmw prune` reclaims it, exactly as a
                # delete-and-re-add would. Probe after changing it — the
                # dimension choices below are the old repository's.
                yield Input(self._source.url, id="edit-source-url")
                yield Label("Name", classes="field-label")
                yield Input(self._source.name, id="edit-source-name")
                yield Label("Type", classes="field-label")
                yield Select([("apt (dists/ hierarchy)", "apt"),
                              ("apt-flat (Packages at the root)", "apt-flat")],
                             value=self._source.provider, allow_blank=False,
                             id="edit-source-provider")
                yield Label("Key group (keys/{label}/)", classes="field-label")
                yield Input(self._label, id="edit-source-label")
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
                yield Label("Key URL (optional; also served at its own path)",
                            classes="field-label")
                yield Input(self._source.gpg_key_url or "", id="edit-source-key-url",
                            placeholder="https://vendor.example/repo.gpg")
                yield Label("Extra files: one URL per line, served at its own path",
                            classes="field-label")
                yield TextArea("".join(f"{u}\n" for u in self._source.extra_files),
                               id="edit-source-extra-files")
                yield Label("Per-suite components: one `suite: comp, comp` per line",
                            classes="field-label")
                yield TextArea(format_overrides(self._suite_overrides),
                               id="edit-source-overrides")
            with Horizontal(id="nav-bar"):
                # Inline, beside the buttons: a refusal to save has to be
                # visible at the place the press happened, not only as a
                # toast that has already faded by the time the user looks.
                yield Label("", id="save-status")
                yield Button("Cancel", id="btn-cancel-source", variant="default")
                yield Button("Save", id="btn-save-source", variant="primary")

    def on_mount(self) -> None:
        self._refresh_summaries()
        self._refresh_key_status()
        # Focusing the first field nudged the scroll region down a row once
        # the form outgrew the screen, hiding the "Repository URL" label.
        container = self.query_one("#edit-source-container")
        self.call_after_refresh(container.scroll_home, animate=False)

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

    @on(Input.Changed, "#edit-source-url")
    def _url_changed(self, event: Input.Changed) -> None:
        url = event.value.strip().rstrip("/")
        if not url or url == self._draft.url:
            return
        self._draft.url = url
        self.query_one("#probe-status", Label).update(
            "[yellow]URL changed — probe to refresh the choices[/]")

    @on(Select.Changed, "#edit-source-provider")
    def _provider_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self._draft.provider = event.value

    # ── probing ──────────────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-probe")
    def _probe_pressed(self, event: "Button.Pressed") -> None:
        event.stop()
        btn = self.query_one("#btn-probe", Button)
        btn.disabled = True
        self.query_one("#probe-status", Label).update("[dim]Probing…[/]")
        self._probe_gen += 1
        self._probe_worker(self._probe_gen)

    @work(thread=True, exit_on_error=False)
    def _probe_worker(self, gen: int) -> None:
        # Widens what is *offered* without moving what is already ticked —
        # the same rule every other probe entry point follows.
        #
        # The body is wrapped because `@work(exit_on_error=False)` *swallows*
        # the exception: without this a raising probe never reaches
        # `_on_probed`, so the button stays disabled and the status stays
        # "Probing…" for the life of the modal, with only escape-and-reopen to
        # recover. `key_modal._fetch_worker` is the template.
        try:
            self._draft.probe()
        except Exception as e:
            self.app.call_from_thread(self._on_probe_failed, e, gen)
            return
        self.app.call_from_thread(self._on_probed, gen)

    def _on_probed(self, gen: int = 0) -> None:
        if gen and gen != self._probe_gen:
            return                                  # a newer probe is in flight
        self.query_one("#btn-probe", Button).disabled = False
        self._refresh_summaries()
        status = self.query_one("#probe-status", Label)
        if self._draft.error:
            status.update(f"[red]✗ {_escape(self._draft.error)}[/]")
        else:
            status.update("[green]✓ probed[/]")

    def _on_probe_failed(self, error: Exception, gen: int) -> None:
        if gen != self._probe_gen:
            return
        self.query_one("#btn-probe", Button).disabled = False
        self.query_one("#probe-status", Label).update(
            f"[red]✗ could not probe: {_escape(str(error) or type(error).__name__)}[/]"
            "  [dim]— check the URL and try again[/]"
        )

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
            self._refuse(
                "Choose at least one suite, or cancel to keep this repository "
                "mirroring the suites it already has."
            )
            return
        url = self.query_one("#edit-source-url", Input).value.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            self._refuse("✗ the repository URL must start with http:// or https://")
            self.query_one("#edit-source-url", Input).focus()
            return
        if config.normalize_url(url) in self._existing_urls:
            self._refuse("✗ another repository already has that URL — edit that one instead.")
            self.query_one("#edit-source-url", Input).focus()
            return
        key_url = self.query_one("#edit-source-key-url", Input).value.strip()
        if key_url and not key_url.startswith(("http://", "https://")):
            self._refuse("✗ the key URL must start with http:// or https://")
            self.query_one("#edit-source-key-url", Input).focus()
            return
        from mmw.core.upstream import upstream_rel
        extra_files = list(dict.fromkeys(
            line.strip() for line in
            self.query_one("#edit-source-extra-files", TextArea).text.splitlines()
            if line.strip()))
        bad = [u for u in extra_files if upstream_rel(u) is None]
        if bad:
            self._refuse(f"✗ {bad[0]} cannot be served at its own path — it needs to be "
                         "an http(s) URL naming a file, with no query string")
            self.query_one("#edit-source-extra-files", TextArea).focus()
            return
        try:
            overrides = parse_overrides(self.query_one("#edit-source-overrides", TextArea).text)
        except ValueError as e:
            self._refuse(f"✗ {e}")
            self.query_one("#edit-source-overrides", TextArea).focus()
            return
        self._draft.url = url
        name = self.query_one("#edit-source-name", Input).value.strip()
        # The name is the one field on this screen that can produce a config
        # the loader rejects, so it is checked here rather than discovered on
        # the next `mmw` invocation.
        if name in self._existing_names:
            self._refuse(
                f"✗ '{name}' is already the name of another repository — "
                "pick a different one."
            )
            self.query_one("#edit-source-name", Input).focus()
            return
        self._draft.name = name or self._draft.name
        self._draft.include_sources = self.query_one("#include-sources-switch", Switch).value
        source = self._draft.to_source()
        source.dist_upgrade = self.query_one("#du-switch", Switch).value
        source.gpg_key_url = key_url or None
        source.suite_overrides = overrides
        source.extra_files = extra_files
        # Blank means "derive it from the URL", the same as leaving it out of
        # mmw.toml — re-validated so that derivation actually runs.
        source.label = self.query_one("#edit-source-label", Input).value.strip()
        try:
            source = config.SourceConfig.model_validate(source.model_dump())
        except Exception as e:
            self._refuse(f"✗ {e}")
            return
        self.dismiss(source)

    def _refuse(self, message: str) -> None:
        """Say no where the press happened, and say it again as a toast."""
        try:
            self.query_one("#save-status", Label).update(f"[red]{_escape(message)}[/]")
        except Exception:
            pass
        self.notify(message, severity="warning")
