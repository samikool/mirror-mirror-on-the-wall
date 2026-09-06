"""Add a repository: probe a URL, or paste a config and probe it the same way.

Two modes, one destination. Whether the repository arrives as a URL or as a
pasted apt config, it becomes a `RepositoryDraft` — the same object, enumerated
the same way, presented through the same three dimension pickers. A pasted
config only supplies a *pre-selection* (and possibly a GPG key), never a
different code path.

A paste naming several repositories (`cat /etc/apt/sources.list.d/*`) produces
several drafts and opens the review pane, which is the same pickers again, one
row per repository.
"""
from pathlib import Path

from rich.markup import escape as _escape
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Input, Label, OptionList, RadioButton, RadioSet, Select, TextArea,
)

from mmw.commands.add import read_input
from mmw.core import config, discover
from mmw.core.apt_sources import parse_apt_sources
from mmw.core.draft import DIMENSIONS, RepositoryDraft, drafts_from_entries, probe_all
from mmw.tui.bulk_review import BulkReviewScreen
from mmw.tui.path_pick import PathPickModal
from mmw.tui.pick import PickModal
from mmw.tui.widgets import Button
from mmw.tui.wizard_steps import WizardStep

_URL_PLACEHOLDER = "https://packages.microsoft.com/repos/code"

# The three axes of an apt repository. One row, one modal, one code path each —
# they differ only in how they are discovered.
_DIM_TITLES = {"suites": "suites", "components": "components", "arches": "architectures"}
_DIMENSIONS = [(d, _DIM_TITLES[d]) for d in DIMENSIONS]

_MODE_HINTS = [
    "[dim]Paste a repository URL — we probe it and show what it publishes.[/]",
    "[dim]Paste a sources.list line or deb822 block — probed the same way, with "
    "the config's own values pre-selected. Several repositories open a review pane.[/]",
]


class StepCustom(WizardStep):
    DEFAULT_CSS = """
    StepCustom { padding: 1 2; height: auto; }
    StepCustom Label { margin-bottom: 0; }
    StepCustom .field-label { color: $text-muted; margin-top: 1; }
    StepCustom Input { margin-bottom: 0; }
    StepCustom Select { margin-bottom: 0; }
    StepCustom RadioSet { height: auto; margin: 1 0; }
    StepCustom #mode-hint { margin-bottom: 1; }
    StepCustom #url-row { height: 3; margin-bottom: 0; }
    StepCustom #url-row Input { width: 1fr; }
    StepCustom #url-row Button { width: auto; min-width: 16; margin-left: 1; }
    StepCustom #paste-area { height: 10; margin-bottom: 0; }
    StepCustom #load-row { height: 3; }
    StepCustom #load-row Button { width: auto; min-width: 18; }
    StepCustom #load-row .load-hint {
        width: 1fr;
        height: 1;
        padding: 1 0 0 2;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    StepCustom #detect-status { margin-top: 1; }
    StepCustom #browse-label { margin-top: 1; }
    StepCustom #browse-list { height: auto; max-height: 10; border: solid $primary-darken-2; }
    StepCustom #bulk-row { height: 3; margin-top: 1; }
    StepCustom #bulk-row Button { width: auto; min-width: 24; }
    StepCustom .dim-row { height: auto; margin-bottom: 0; }
    StepCustom .dim-row Button { min-width: 0; width: auto; }
    StepCustom .dim-summary {
        padding: 1 0 0 2;
        color: $accent;
        width: 1fr;
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    """

    def __init__(self, existing_urls: "set[str] | None" = None):
        super().__init__()
        # Repository identities already configured — a bundle row that matches
        # one is a merge, not a new source, and says so.
        self._existing_urls = existing_urls or set()

    def compose(self) -> ComposeResult:
        yield Label("[bold]Add repository[/]")
        with RadioSet(id="mode-select"):
            yield RadioButton("Probe URL", value=True, id="mode-url")
            yield RadioButton("Paste config", id="mode-paste")
        yield Label(_MODE_HINTS[0], id="mode-hint")
        yield Label("  [dim]Repository URL[/]", classes="field-label", id="url-label")
        with Horizontal(id="url-row"):
            yield Input(placeholder=_URL_PLACEHOLDER, id="custom-url")
            yield Button("Probe", id="btn-detect", variant="primary")
        yield TextArea(id="paste-area", show_line_numbers=False)
        # Reading the files ourselves beats a hand-rolled `cat`: mmw joins them
        # with blank lines, so records can never run together (see
        # `commands.add.read_input`).
        with Horizontal(id="load-row"):
            yield Button("Load from files…", id="btn-load", variant="default")
            yield Label("[dim]a .sources/.list file, or a whole sources.list.d[/]",
                        classes="load-hint")
        yield Label("", id="detect-status")
        # Shown when the URL is a host root rather than a repository:
        # archive.ubuntu.com/ holds ubuntu/, packages.microsoft.com/ holds
        # dozens. Picking one descends into it and probes again.
        yield Label("  [dim]Not a repository — pick one to open[/]", id="browse-label")
        yield OptionList(id="browse-list")
        # Several repositories in one paste: the pickers below can only express
        # one, so the bundle goes to its own pane.
        with Horizontal(id="bulk-row"):
            yield Button("Review repositories…", id="btn-bulk", variant="primary")
        with Vertical(id="fields-container"):
            yield Label("  [dim]Name[/]", classes="field-label")
            yield Input(placeholder="derived from the URL", id="custom-name")
            yield Label("  [dim]Provider[/]", classes="field-label")
            yield Select(
                [
                    ("APT  (InRelease / Packages)", "apt"),
                    ("APT flat  (Packages at root, no dists/)", "apt-flat"),
                ],
                id="custom-provider",
                value="apt",
                allow_blank=False,
            )
            for dim, title in _DIMENSIONS:
                with Horizontal(id=f"{dim}-row", classes="dim-row"):
                    yield Button(f"Choose {title}…", id=f"btn-{dim}", variant="primary")
                    yield Label("", id=f"{dim}-summary", classes="dim-summary")

    def on_mount(self) -> None:
        self._probe_gen = 0
        # The one repository these pickers edit. A paste naming several holds
        # them in `_bundle` instead and hands them to the review pane.
        self._draft: RepositoryDraft | None = None
        self._bundle: list[RepositoryDraft] = []
        self.query_one("#paste-area").display = False
        self.query_one("#load-row").display = False
        self.query_one("#detect-status").display = False
        self.query_one("#fields-container").display = False
        self.query_one("#bulk-row").display = False
        self._hide_browse()
        for dim, _ in _DIMENSIONS:
            self.query_one(f"#{dim}-row").display = False

    # ── mode ─────────────────────────────────────────────────────────────────

    @on(RadioSet.Changed, "#mode-select")
    def _mode_changed(self, event: "RadioSet.Changed") -> None:
        is_paste = event.index == 1
        self.query_one("#url-label").display = not is_paste
        self.query_one("#url-row").display = not is_paste
        self.query_one("#paste-area").display = is_paste
        self.query_one("#load-row").display = is_paste
        self.query_one("#mode-hint", Label).update(_MODE_HINTS[event.index])
        if not is_paste:
            self.query_one("#custom-url", Input).value = ""
        self._reset()

    def _reset(self) -> None:
        self._draft = None
        self._bundle = []
        self.query_one("#detect-status").display = False
        self.query_one("#fields-container").display = False
        self.query_one("#bulk-row").display = False
        self._hide_browse()
        for dim, _ in _DIMENSIONS:
            self.query_one(f"#{dim}-row").display = False

    # ── one draft drives the fields ──────────────────────────────────────────

    def _show_draft(self, draft: RepositoryDraft) -> None:
        """Point the fields at a draft and render every dimension it carries."""
        self._draft = draft
        self._bundle = []
        self.query_one("#custom-url", Input).value = draft.url
        self.query_one("#custom-provider", Select).value = draft.provider
        self.query_one("#custom-name", Input).value = draft.name
        self.query_one("#bulk-row").display = False
        self.query_one("#fields-container").display = True
        self._refresh_dimensions()

    def _refresh_dimensions(self) -> None:
        for dim, _ in _DIMENSIONS:
            self._update_dimension_summary(dim)

    # ── probe a URL ──────────────────────────────────────────────────────────

    @on(Input.Submitted, "#custom-url")
    def _url_submitted(self, event: "Input.Submitted") -> None:
        if event.value.strip():
            self._probe(event.value.strip())

    @on(Button.Pressed, "#btn-detect")
    def _detect_pressed(self, event: "Button.Pressed") -> None:
        event.stop()
        url = self.query_one("#custom-url", Input).value.strip()
        if url:
            self._probe(url)

    def _probe(self, url: str) -> None:
        status = self.query_one("#detect-status", Label)
        status.update("  [dim]Probing…[/]")
        status.display = True
        self.query_one("#fields-container").display = False
        self.query_one("#bulk-row").display = False
        self._hide_browse()
        self._probe_gen += 1
        self._detect_worker(url, self._probe_gen)

    @work(thread=True, exit_on_error=False)
    def _detect_worker(self, url: str, gen: int) -> None:
        result = discover.detect_repository(url)
        self.app.call_from_thread(self._on_detect, url, result, gen)

    def _on_detect(self, url: str, result: "discover.DetectResult", gen: int) -> None:
        if gen != self._probe_gen:
            return                                  # a newer probe is in flight
        status = self.query_one("#detect-status", Label)
        if result.provider is None:
            if result.candidates:
                status.update(
                    f"  [yellow]Not a repository — {len(result.candidates)} "
                    "director(y/ies) here[/]"
                )
                self._show_browse(result.candidates)
                return
            status.update(f"  [red]✗ Not an apt repository ({_escape(result.error)})[/]")
            return

        self._hide_browse()
        status.update(f"  [green]✓ {_escape(result.signal)}[/]")
        draft = RepositoryDraft(url=url, provider=result.provider)
        if result.provider == "apt":
            # Nothing preselected: archive.ubuntu.com publishes 55 suites, and
            # preselecting them would turn one Enter into a multi-terabyte sync.
            draft.set_dimension("suites", result.suites, chosen=[], enumerated=True)
        self._show_draft(draft)

    # ── load a config off disk ───────────────────────────────────────────────

    @on(Button.Pressed, "#btn-load")
    def _load_pressed(self, event: "Button.Pressed") -> None:
        event.stop()
        self._load_files()

    @work
    async def _load_files(self) -> None:
        picked = await self.app.push_screen_wait(PathPickModal(
            "Load apt configuration",
            "pick a .sources / .list file, or a directory of them",
            "/etc/apt/sources.list.d",
            dirs_only=False,
            suffixes=(".sources", ".list"),
        ))
        if picked is None:
            return                                  # cancelled: leave it alone
        try:
            text = read_input([Path(picked)], None)
        except Exception as e:
            self.app.notify(f"Could not read {picked}: {e}", severity="error")
            return
        # Setting the text fires TextArea.Changed, so this lands in exactly the
        # same place a paste does.
        self.query_one("#paste-area", TextArea).text = text

    # ── paste a config ───────────────────────────────────────────────────────

    @on(TextArea.Changed, "#paste-area")
    def _paste_changed(self, event: "TextArea.Changed") -> None:
        text = event.text_area.text.strip()
        status = self.query_one("#detect-status", Label)
        if not text:
            self._reset()
            return
        entries = parse_apt_sources(text)
        if not entries:
            if sum(1 for ln in text.splitlines() if ln.strip()) >= 2:
                status.update(
                    "  [dim]Not recognized — expected a sources.list line or deb822 block.[/]"
                )
                status.display = True
            self.query_one("#fields-container").display = False
            self.query_one("#bulk-row").display = False
            return

        drafts = drafts_from_entries(entries)
        keyed = sum(1 for d in drafts if d.gpg_key)
        key_note = f"  [dim]+ {keyed} GPG key(s)[/]" if keyed else ""
        self._probe_gen += 1

        if len(drafts) > 1:
            self._draft = None
            self._bundle = drafts
            for dim, _ in _DIMENSIONS:
                self.query_one(f"#{dim}-row").display = False
            status.update(
                f"  [green]✓ {len(entries)} entries across {len(drafts)} repositories[/]"
                f"{key_note}  [dim]probing…[/]"
            )
            status.display = True
            self.query_one("#fields-container").display = False
            self.query_one("#bulk-row").display = True
            self._bulk_probe_worker(drafts, self._probe_gen)
            return

        draft = drafts[0]
        status.update(f"  [green]✓ {draft.provider.upper()} — {_escape(draft.url)}[/]{key_note}")
        status.display = True
        # The config's values are a *pre-selection*: show them at once, then
        # probe so everything the repository publishes becomes visible without
        # changing what is ticked.
        self._show_draft(draft)
        if draft.provider == "apt":
            self._enrich_worker(draft, self._probe_gen)

    @work(thread=True, exit_on_error=False)
    def _enrich_worker(self, draft: RepositoryDraft, gen: int) -> None:
        """Widen a pasted config's dimensions to everything the repo publishes.

        The config states a filter; probing finds the full set it filtered
        *from*, so the pickers can offer the rest without changing what is
        selected — exactly what the URL path does, one step later.
        """
        draft.probe()
        self.app.call_from_thread(self._on_enriched, draft, gen)

    def _on_enriched(self, draft: RepositoryDraft, gen: int) -> None:
        if gen == self._probe_gen and draft is self._draft:
            self._refresh_dimensions()

    # ── a paste naming several repositories ──────────────────────────────────

    @work(thread=True, exit_on_error=False)
    def _bulk_probe_worker(self, drafts: list[RepositoryDraft], gen: int) -> None:
        """Probe every repository in the bundle at once.

        A DGX sources tree names a dozen; probing them one after another would
        take a dozen round-trip stacks before the pane could open.
        """
        probe_all(drafts)
        self.app.call_from_thread(self._on_bulk_probed, drafts, gen)

    def _on_bulk_probed(self, drafts: list[RepositoryDraft], gen: int) -> None:
        if gen != self._probe_gen or drafts is not self._bundle:
            return
        ok = sum(1 for d in drafts if not d.error)
        note = "" if ok == len(drafts) else f"  [yellow]{len(drafts) - ok} could not be probed[/]"
        self.query_one("#detect-status", Label).update(
            f"  [green]✓ {len(drafts)} repositories ready to review[/]{note}"
        )

    @on(Button.Pressed, "#btn-bulk")
    def _bulk_pressed(self, event: "Button.Pressed") -> None:
        event.stop()
        self._open_review()

    @work
    async def _open_review(self) -> None:
        kept = await self.app.push_screen_wait(BulkReviewScreen(self._bundle, self._existing_urls))
        if kept is None:
            return                                  # cancelled: leave it alone
        self._bundle = kept
        self.query_one("#detect-status", Label).update(
            f"  [green]✓ {len(kept)} repositor{'y' if len(kept) == 1 else 'ies'} selected[/]"
        )

    # ── browsing a host that is not itself a repository ──────────────────────

    def _hide_browse(self) -> None:
        self.query_one("#browse-label").display = False
        self.query_one("#browse-list").display = False

    def _show_browse(self, candidates: "list[str]") -> None:
        ol = self.query_one("#browse-list", OptionList)
        ol.clear_options()
        ol.add_options(candidates)
        self.query_one("#browse-label").display = True
        ol.display = True

    @on(OptionList.OptionSelected, "#browse-list")
    def _browse_into(self, event) -> None:
        """Descend one level and probe again — never a recursive crawl."""
        inp = self.query_one("#custom-url", Input)
        inp.value = inp.value.strip().rstrip("/") + "/" + str(event.option.prompt)
        self._hide_browse()
        self._probe(inp.value)

    # ── dimensions: one implementation for suites, components and arches ─────

    def _update_dimension_summary(self, dim: str) -> None:
        draft = self._draft
        if draft is None:
            self.query_one(f"#{dim}-row").display = False
            return
        available = draft.available[dim]
        self.query_one(f"#{dim}-row").display = bool(available)
        chosen = draft.chosen[dim]
        if not chosen:
            text = "[dim]none selected[/]"
        elif len(chosen) == len(available):
            text = f"[italic]all[/] [dim]({len(available)})[/]"
        elif len(chosen) <= 4:
            text = f"{', '.join(chosen)}  [dim]of {len(available)}[/]"
        else:
            text = f"{', '.join(chosen[:4])}  [dim]+{len(chosen) - 4} of {len(available)}[/]"
        self.query_one(f"#{dim}-summary", Label).update(text)

    @work
    async def _choose_dimension(self, dim: str, title: str) -> None:
        draft = self._draft
        if draft is None:
            return
        picked = await self.app.push_screen_wait(PickModal(
            f"Choose {title}", draft.url, draft.available[dim], draft.chosen[dim],
        ))
        if picked is None:
            return                                  # cancelled: leave it alone
        draft.chosen[dim] = picked
        self._update_dimension_summary(dim)
        if dim == "suites" and picked:
            self._probe_gen += 1
            self._suite_dimensions_worker(draft, picked[0], self._probe_gen)

    @on(Button.Pressed, "#btn-suites")
    def _pick_suites(self) -> None:
        self._choose_dimension("suites", "suites")

    @on(Button.Pressed, "#btn-components")
    def _pick_components(self) -> None:
        self._choose_dimension("components", "components")

    @on(Button.Pressed, "#btn-arches")
    def _pick_arches(self) -> None:
        self._choose_dimension("arches", "architectures")

    @work(thread=True, exit_on_error=False)
    def _suite_dimensions_worker(self, draft: RepositoryDraft, suite: str, gen: int) -> None:
        """Components and architectures follow from a chosen suite."""
        draft.probe_suite(suite)
        self.app.call_from_thread(self._on_suite_dimensions, draft, gen)

    def _on_suite_dimensions(self, draft: RepositoryDraft, gen: int) -> None:
        if gen == self._probe_gen and draft is self._draft:
            self._refresh_dimensions()

    # ── building the result ──────────────────────────────────────────────────

    def get_sources(self) -> "list[config.SourceConfig]":
        if self._bundle:
            return [d.to_source() for d in self._bundle]

        draft = self._draft
        if draft is None:
            self.app.notify("Probe a URL or paste a config first.", severity="warning")
            return []

        url = self.query_one("#custom-url", Input).value.strip().rstrip("/")
        if not url:
            self.app.notify("URL is required.", severity="warning")
            return []
        draft.url = url
        draft.provider = str(self.query_one("#custom-provider", Select).value)
        # A blank name falls back to the shared derivation, never a second one —
        # the TUI and `mmw add` must name the same repository the same way.
        draft.name = (self.query_one("#custom-name", Input).value.strip()
                      or config.derive_name(url, suites=draft.stored("suites") or None))
        if draft.provider == "apt" and not draft.chosen["suites"]:
            self.app.notify("Choose at least one suite.", severity="warning")
            return []
        return [draft.to_source()]
