"""Edit an already-configured repository.

The Networks tab has full CRUD; the Manage tab only ever had Add and Delete.
That is a real gap, not a cosmetic one: `config.merge_sources()` is
union-only by design (`_union_or_all()` returns `[]` when either side is
empty, deliberately, so importing one explicit arch can never *narrow* a
repository mirroring all of them). Re-running the add flow on
top of an existing source can therefore only ever widen it. Narrowing a
selection — dropping an arch, dropping a suite, restricting components — had
no route through the TUI at all; only delete-then-re-add or hand-editing
`mmw.toml` could do it. This page is that missing route: it writes the
edited `SourceConfig` directly over the existing entry (`repositories.replace_source` replaces
by identity, never merges), so a narrower selection actually sticks.

Built on the same `RepositoryDraft` every other add-a-repository door uses
(`RepositoryDraft.from_source()` seeds one from an existing entry), so probing
and the "empty means all" rule are the same code as everywhere else — not a
second implementation that could drift from it.
"""
from textual import on, work
from textual.app import ComposeResult
from textual.markup import escape
from textual.widgets import Checkbox, Input, Select, TextArea

from mmw.core import config
from mmw.core.draft import DIMENSIONS, RepositoryDraft
from mmw.tui.key_modal import KeyPage, apply_key, key_summary
from mmw.tui.pick import PickPage
from mmw.tui.repositories import edited_source, format_overrides, parse_overrides  # noqa: F401
from mmw.tui.ui.form import (
    ActionField, ChoiceField, Form, Heading, TextAreaField, TextField, ToggleField,
)
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button

_DIM_TITLES = {"suites": "suites", "components": "components", "arches": "architectures"}


class EditSourcePage(Page):
    """Edit one configured repository's dimensions, key and flags.

    Dismisses with the edited `SourceConfig`, or `None` (Esc / Cancel) — the
    same "None means leave it alone" convention as every other result page.
    """

    def __init__(self, source: config.SourceConfig,
                 existing_names: "set[str] | None" = None,
                 existing_urls: "set[str] | None" = None):
        super().__init__()
        self.TITLE = source.name
        self.subtitle = source.url
        self._source = source
        # The *other* repositories' identities and names: this one's URL or
        # name moved onto theirs would write a config the loader refuses.
        self._existing_urls = set(existing_urls or ()) - {source.identity}
        self._existing_names = set(existing_names or ()) - {source.name}
        self._draft = RepositoryDraft.from_source(source)
        # Whether this repository arrived mirroring *all* suites — see
        # `repositories.edited_source`.
        self._suites_started_empty = not source.suites
        # A stale probe must not clobber a selection the user has moved past.
        self._probe_gen = 0

    def compose_body(self) -> ComposeResult:
        s = self._source
        yield Form(
            Heading("Repository"),
            # Editable. The URL is identity, so a different one is a different
            # repository: the old one's pool content becomes orphaned and
            # `mmw prune` reclaims it. Probe after changing it.
            TextField("URL", s.url, id="edit-source-url",
                      hint="Changing it makes this a different repository — probe after changing."),
            TextField("Name", s.name, id="edit-source-name"),
            ChoiceField("Type", [("apt (dists/ hierarchy)", "apt"),
                                 ("apt-flat (Packages at the root)", "apt-flat")],
                        s.provider, id="edit-source-provider"),
            TextField("Key group", s.label, id="edit-source-label",
                      hint="keys/{label}/ — blank derives it from the URL."),
            Heading("What to mirror"),
            *[ActionField(_DIM_TITLES[d].capitalize(), "", "Choose…", id=f"btn-{d}",
                          summary_id=f"{d}-summary") for d in DIMENSIONS],
            ActionField("Choices", "", "Probe", id="btn-probe", summary_id="probe-status",
                        hint="List what the repository serves, to choose from it."),
            ToggleField("deb-src", s.include_sources, id="include-sources-switch",
                        hint="Also mirror source packages."),
            ToggleField("dist-upgrade", s.dist_upgrade, id="du-switch",
                        hint="Enable Ubuntu dist-upgrade support for this repository."),
            Heading("Signing key"),
            ActionField("Key", "", "Key…", id="btn-key", summary_id="key-status"),
            TextField("Key URL", s.gpg_key_url or "", id="edit-source-key-url",
                      placeholder="https://vendor.example/repo.gpg",
                      hint="Optional; the key is also served at its own path."),
            Heading("Extras"),
            TextAreaField("Extra files", "".join(f"{u}\n" for u in s.extra_files),
                          id="edit-source-extra-files",
                          hint="One URL per line, each served at its own path."),
            TextAreaField("Per-suite components", format_overrides(s.suite_overrides),
                          id="edit-source-overrides",
                          hint="One `suite: comp, comp` per line."),
        )

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-cancel-source", compact=True)
        yield Button("Save", id="btn-save-source", variant="primary", compact=True)

    def on_mount(self) -> None:
        self._refresh_summaries()
        self._refresh_key_status()

    # ── dimension summaries ──────────────────────────────────────────────────

    def _refresh_summaries(self) -> None:
        for dim in DIMENSIONS:
            self.query_one(f"#field-btn-{dim}", ActionField).set_summary(
                escape(self._draft.summary(dim)))

    @work
    async def _choose_dimension(self, dim: str) -> None:
        picked = await self.app.push_screen_wait(PickPage(
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
        self._probe_summary("[$mmw-warn]URL changed — probe to refresh the choices[/]")

    @on(Select.Changed, "#edit-source-provider")
    def _provider_changed(self, event: Select.Changed) -> None:
        if isinstance(event.value, str):
            self._draft.provider = event.value

    # ── probing ──────────────────────────────────────────────────────────────

    def _probe_summary(self, text: str) -> None:
        self.query_one("#field-btn-probe", ActionField).set_summary(text)

    @on(Button.Pressed, "#btn-probe")
    def _probe_pressed(self, event: "Button.Pressed") -> None:
        event.stop()
        self.query_one("#btn-probe", Button).disabled = True
        self._probe_summary("[dim]Probing…[/]")
        self._probe_gen += 1
        self._probe_worker(self._probe_gen)

    @work(thread=True, exit_on_error=False)
    def _probe_worker(self, gen: int) -> None:
        # Widens what is *offered* without moving what is already ticked.
        # Wrapped because `exit_on_error=False` swallows the exception: without
        # this a raising probe never reaches `_on_probed`, and the button stays
        # disabled for the life of the page.
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
        if self._draft.error:
            self._probe_summary(f"[$mmw-error]✗ {escape(self._draft.error)}[/]")
        else:
            self._probe_summary("[$mmw-ok]✓ probed[/]")

    def _on_probe_failed(self, error: Exception, gen: int) -> None:
        if gen != self._probe_gen:
            return
        self.query_one("#btn-probe", Button).disabled = False
        self._probe_summary(
            f"[$mmw-error]✗ could not probe: {escape(str(error) or type(error).__name__)}[/]"
            "  [dim]— check the URL and try again[/]")

    # ── key ──────────────────────────────────────────────────────────────────

    def _refresh_key_status(self) -> None:
        # The Key URL field is what gets saved, so it is what this reads: a
        # key configured only by URL is a key, not "no key".
        url = self.query_one("#edit-source-key-url", Input).value.strip()
        self.query_one("#field-btn-key", ActionField).set_summary(
            key_summary(self._draft, url))

    @on(Input.Changed, "#edit-source-key-url")
    def _key_url_changed(self) -> None:
        self._refresh_key_status()

    @on(Button.Pressed, "#btn-key")
    def _key_pressed(self, event: "Button.Pressed") -> None:
        event.stop()
        self._open_key()

    @work
    async def _open_key(self) -> None:
        result = await self.app.push_screen_wait(KeyPage(
            self._draft.name, self._draft.gpg_key_name, self._draft.gpg_key,
        ))
        if not apply_key(self._draft, result):
            return                                  # cancelled: leave it alone
        _, _, url = result
        key_url = self.query_one("#edit-source-key-url", Input)
        if url:
            # Shown, not hidden: sync mirrors the key at this path too.
            key_url.value = url
        elif not self._draft.gpg_key:
            # Cleared. The URL is a key too: left in place, the next sync
            # would fetch the key just cleared straight back.
            key_url.value = ""
        self._refresh_key_status()

    # ── result ───────────────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-cancel-source")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-save-source")
    def _save(self) -> None:
        q = self.query_one
        source, message, focus = edited_source(
            self._draft, original=self._source,
            url=q("#edit-source-url", Input).value,
            name=q("#edit-source-name", Input).value,
            label=q("#edit-source-label", Input).value,
            key_url=q("#edit-source-key-url", Input).value,
            include_sources=q("#include-sources-switch", Checkbox).value,
            dist_upgrade=q("#du-switch", Checkbox).value,
            extra_files=q("#field-edit-source-extra-files", TextAreaField).lines(),
            overrides_text=q("#edit-source-overrides", TextArea).text,
            other_names=self._existing_names, other_urls=self._existing_urls,
            suites_started_empty=self._suites_started_empty,
        )
        if source is None:
            # Said where the press happened, and again as a toast.
            self.set_status(message, "error")
            self.notify(message, severity="warning")
            if focus:
                self.query_one(f"#{focus}").focus()
            return
        self.dismiss(source)
