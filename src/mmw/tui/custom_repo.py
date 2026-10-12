"""Add a repository: probe a URL, or paste a config and probe it the same way.

Two modes, one destination. Whether the repository arrives as a URL or as a
pasted apt config, it becomes a `SourceDraft` — the same object, enumerated
the same way, presented through the same three dimension pickers. A pasted
config only supplies a *pre-selection* (and possibly a GPG key), never a
different code path.

A paste naming several repositories (`cat /etc/apt/sources.list.d/*`) produces
several drafts and opens the review pane, which is the same pickers again, one
row per repository.
"""
from pathlib import Path

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.markup import escape
from textual.widgets import (
    Input, Label, OptionList, RadioButton, RadioSet, Select, TextArea,
)

from mmw.commands.add import read_input
from mmw.core import config, discover, probers
from mmw.core.apt_sources import (
    AptEntry, detect_arch_placeholder, is_sources_file_url, parse_apt_sources,
    remember_sources_url,
)
from mmw.core.draft import (
    DIMENSIONS, SourceDraft, drafts_from_entries, probe_all, resolve_placeholders,
)
from mmw.tui.bulk_review import BulkReviewPage
from mmw.tui.key_modal import KeyPage, apply_key, key_summary
from mmw.tui.path_pick import PathPickPage
from mmw.tui.pick import PickPage
from mmw.tui.ui.form import ActionField, ChoiceField, Form, Heading, TextField
from mmw.tui.ui.widgets import Button
from mmw.tui.wizard_steps import WizardStep

_URL_PLACEHOLDER = "https://packages.microsoft.com/repos/code"

# The three axes of an apt repository. One row, one modal, one code path each —
# they differ only in how they are discovered.
_DIM_TITLES = {"suites": "suites", "components": "components", "arches": "architectures"}
# A paste is typed or pasted character by character; wait for it to settle
# before spending network round trips on it.
_PASTE_DEBOUNCE = 0.5

_DIMENSIONS = [(d, _DIM_TITLES[d]) for d in DIMENSIONS]

_MODE_HINTS = [
    "[dim]A repository URL, or a .list / .sources file's URL — we probe it.[/]",
    "[dim]Paste a sources.list line or deb822 block — probed the same way, with "
    "the config's own values pre-selected. Several repositories open a review pane.[/]",
]


class StepCustom(WizardStep):

    def __init__(self, existing_urls: "set[str] | None" = None):
        super().__init__()
        # Repository identities already configured — a bundle row that matches
        # one is a merge, not a new source, and says so.
        self._existing_urls = existing_urls or set()

    def compose(self) -> ComposeResult:
        yield Heading("Add a repository")
        with RadioSet(id="mode-select", compact=True):
            yield RadioButton("Probe URL", value=True, id="mode-url")
            yield RadioButton("Paste config", id="mode-paste")
        yield Label(_MODE_HINTS[0], id="mode-hint")
        yield Label("Repository URL", id="url-label")
        with Horizontal(id="url-row"):
            yield Input(placeholder=_URL_PLACEHOLDER, id="custom-url", compact=True)
            yield Button("Probe", id="btn-detect", variant="primary", compact=True)
        yield TextArea(id="paste-area", show_line_numbers=False, compact=True)
        # Reading the files ourselves beats a hand-rolled `cat`: mmw joins them
        # with blank lines, so records can never run together (see
        # `commands.add.read_input`).
        with Horizontal(id="load-row"):
            yield Button("Load from files…", id="btn-load", compact=True)
            yield Label("[dim]a .sources/.list file, or a whole sources.list.d[/]",
                        classes="load-hint")
        yield Label("", id="detect-status")
        # Shown when the URL is a host root rather than a repository:
        # archive.ubuntu.com/ holds ubuntu/, packages.microsoft.com/ holds
        # dozens. Picking one descends into it and probes again.
        yield Label("[dim]Not a repository — pick one to open[/]", id="browse-label")
        yield OptionList(id="browse-list", compact=True)
        # Several repositories in one paste: the pickers below can only express
        # one, so the bundle goes to its own page.
        with Horizontal(id="bulk-row"):
            yield Button("Review repositories…", id="btn-bulk", variant="primary", compact=True)
        with Vertical(id="fields-container"):
            yield Form(
                TextField("Name", "", id="custom-name", placeholder="derived from the URL"),
                ChoiceField("Provider", [
                    ("APT  (InRelease / Packages)", "apt"),
                    ("APT flat  (Packages at root, no dists/)", "apt-flat"),
                ], "apt", id="custom-provider"),
                # field_id keeps the `#{dim}-row` ids the logic below toggles.
                *[ActionField(title.capitalize(), "", "Choose…", id=f"btn-{dim}",
                              summary_id=f"{dim}-summary", field_id=f"{dim}-row")
                  for dim, title in _DIMENSIONS],
                # The same key page editing uses: scan a directory, fetch a URL
                # (kept as the key URL, so it is mirrored at its own path), or paste.
                ActionField("Key", "", "Key…", id="btn-key", summary_id="key-status",
                            hint="The repository's GPG key — fetched, pasted, or found "
                                 "in a directory of keyrings."),
            )

    def on_mount(self) -> None:
        self._probe_gen = 0
        # Pending debounce for the paste box — see `_paste_changed`.
        self._paste_timer = None
        # True while the bundle is still being probed: the review pane reads
        # the drafts, and `probe_all` mutates them from several threads, so
        # opening it early showed stale dimensions and could hand back a draft
        # caught mid-`set_dimension`.
        self._bulk_probing = False
        # The one repository these pickers edit. A paste naming several holds
        # them in `_bundle` instead and hands them to the review pane.
        self._draft: SourceDraft | None = None
        self._bundle: list[SourceDraft] = []
        # Set while the repositories shown came from a sources file fetched
        # by URL (Probe URL on a `.list`): each keeps it as an extra file.
        self._sources_url: str | None = None
        # True once a paste has named several repositories, so `get_sources()`
        # can tell "the bundle was emptied in the review pane" apart from
        # "nothing was ever pasted" — two states that produced the same
        # unhelpful refusal.
        self._had_bundle = False
        self.query_one("#paste-area").display = False
        self.query_one("#load-row").display = False
        self.query_one("#detect-status").display = False
        self.query_one("#fields-container").display = False
        self.query_one("#bulk-row").display = False
        self._hide_browse()
        for dim, _ in _DIMENSIONS:
            self.query_one(f"#{dim}-row").display = False

    def focus_first(self) -> None:
        """Focus the visible input: in paste mode the URL row is hidden, and
        focus there let typing/Enter silently re-probe and drop the paste."""
        row_shown = self.query_one("#url-row").display
        self.query_one("#custom-url" if row_shown else "#paste-area").focus()

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
            # Clear the box too. `_reset()` dropped the *draft* but left the
            # pasted text sitting on screen, so switching away and back showed
            # a config, no Review button, and "Probe a URL or paste a config
            # first." from Next — with the config demonstrably right there.
            # Assigning "" fires TextArea.Changed, which lands in `_reset()`
            # through the ordinary path.
            self.query_one("#paste-area", TextArea).text = ""
        self._reset()

    def _reset(self) -> None:
        self._sources_url = None
        self._draft = None
        self._bundle = []
        self._had_bundle = False
        self.query_one("#detect-status").display = False
        self.query_one("#fields-container").display = False
        self.query_one("#bulk-row").display = False
        self._hide_browse()
        for dim, _ in _DIMENSIONS:
            self.query_one(f"#{dim}-row").display = False

    # ── one draft drives the fields ──────────────────────────────────────────

    def _show_draft(self, draft: SourceDraft) -> None:
        """Point the fields at a draft and render every dimension it carries."""
        self._draft = draft
        self._bundle = []
        self.query_one("#custom-url", Input).value = draft.url
        self.query_one("#custom-provider", Select).value = draft.provider
        self.query_one("#custom-name", Input).value = draft.name
        self.query_one("#bulk-row").display = False
        self.query_one("#fields-container").display = True
        self._refresh_dimensions()
        self._refresh_key()

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
        self.query_one("#fields-container").display = False
        self.query_one("#bulk-row").display = False
        self._hide_browse()
        self._probe_gen += 1
        self._sources_url = None

        if is_sources_file_url(url):
            # NVIDIA-style: the install docs `curl` a `.list`. It is a config,
            # not a repository — read it exactly like a paste.
            status.update("[dim]Reading the sources file…[/]")
            status.display = True
            self._sources_file_worker(url, self._probe_gen)
            return

        placeholder = detect_arch_placeholder(url)
        if placeholder:
            # apt's own $(ARCH)/$ARCH substitution has no local machine to run
            # on here (see core/apt_sources.py) — a bare URL typed into this
            # field never goes through the config parser at all, so it needs
            # its own check for the same token. Resolving it is apt-specific
            # (probers.py deliberately doesn't generalize this), and can turn
            # one URL into several repositories, so it bypasses the ordinary
            # single-DetectResult dispatch entirely.
            status.update("[dim]Resolving $(ARCH)…[/]")
            status.display = True
            self._resolve_url_placeholder_worker(url, placeholder, self._probe_gen)
            return

        status.update("[dim]Probing…[/]")
        status.display = True
        self._detect_worker(url, self._probe_gen)


    def _bulk_probe_done(self) -> None:
        self._bulk_probing = False
        try:
            btn = self.query_one("#btn-bulk", Button)
            btn.disabled = False
            btn.label = "Review repositories…"
        except Exception:
            pass

    def _worker_failed(self, where: str, exc: BaseException) -> None:
        """Put a failed probe back on screen instead of leaving a dead spinner.

        `@work(thread=True, exit_on_error=False)` logs the exception and drops
        it, so the `call_from_thread` that re-enables the control never ran: a
        DNS failure or an httpx error the discover layer does not catch left
        "Probing…" on screen forever with the button disabled, recoverable only
        by escaping the screen. `key_modal._fetch_worker` was the only worker
        that already handled this; this is that shape, shared.
        """
        name = type(exc).__name__
        detail = str(exc).strip() or name
        status = self.query_one("#detect-status", Label)
        status.update(f"[$mmw-error]✗ {where} failed:[/] {escape(str(detail))}")
        status.display = True
        self._set_busy(False)
        self._bulk_probe_done()

    def _set_busy(self, busy: bool) -> None:
        try:
            self.query_one("#btn-detect", Button).disabled = busy
        except Exception:
            pass

    @work(thread=True, exit_on_error=False)
    def _detect_worker(self, url: str, gen: int) -> None:
        try:
            result = probers.probe_url(url)
            self.app.call_from_thread(self._on_detect, url, result, gen)
        except Exception as exc:
            self.app.call_from_thread(self._worker_failed, "probe", exc)

    @work(thread=True, exit_on_error=False)
    def _resolve_url_placeholder_worker(self, url: str, placeholder: str, gen: int) -> None:
        try:
            drafts = []
            for arch, result in discover.probe_arch_placeholder(url, placeholder):
                draft = SourceDraft(url=url.replace(placeholder, arch), provider=result.provider)
                if result.provider == "apt":
                    draft.set_dimension("suites", result.suites, chosen=[], enumerated=True)
                drafts.append(draft)
            self.app.call_from_thread(self._on_url_placeholder_resolved, drafts, gen)
        except Exception as exc:
            self.app.call_from_thread(self._worker_failed, "architecture probe", exc)

    def _on_url_placeholder_resolved(self, drafts: list[SourceDraft], gen: int) -> None:
        if gen != self._probe_gen:
            return                                  # a newer probe is in flight
        if not drafts:
            self._draft = None
            self._bundle = []
            status = self.query_one("#detect-status", Label)
            status.update(
                "[$mmw-error]✗ $(ARCH) resolved to no architecture — repository unreachable?[/]"
            )
            status.display = True
            return
        self._display_drafts(len(drafts), drafts)

    def _on_detect(self, url: str, result: "discover.DetectResult", gen: int) -> None:
        if gen != self._probe_gen:
            return                                  # a newer probe is in flight
        status = self.query_one("#detect-status", Label)
        if result.provider is None:
            if result.candidates:
                status.update(
                    f"[$mmw-warn]Not a repository — {len(result.candidates)} "
                    "director(y/ies) here[/]"
                )
                self._show_browse(result.candidates)
                return
            status.update(f"[$mmw-error]✗ Not an apt repository ({escape(result.error)})[/]")
            return

        self._hide_browse()
        status.update(f"[$mmw-ok]✓ {escape(result.signal)}[/]")
        draft = SourceDraft(url=url, provider=result.provider)
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
        picked = await self.app.push_screen_wait(PathPickPage(
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
        """Debounced: `TextArea.Changed` fires once per character.

        Every change that parses re-entered `_display_drafts`, which starts
        probe workers — several HTTP round trips each — and the generation
        token only suppressed the stale *UI update*, never the I/O already in
        flight. Measured: typing one 48-character `deb …` line produced ten
        calls into `discover.list_suites` against the same host; a
        twelve-repository paste multiplies that by twelve and looks like a
        scrape from the other end.
        """
        if self._paste_timer is not None:
            self._paste_timer.stop()
        text = event.text_area.text
        self._paste_timer = self.set_timer(
            _PASTE_DEBOUNCE, lambda: self._paste_settled(text)
        )

    def _paste_settled(self, text: str) -> None:
        self._paste_timer = None
        self._sources_url = None                    # typed, not fetched
        self._apply_paste(text)

    @work(thread=True, exit_on_error=False)
    def _sources_file_worker(self, url: str, gen: int) -> None:
        try:
            text = read_input(None, url)
        except Exception as exc:
            self.app.call_from_thread(self._worker_failed, "sources file", exc)
            return
        self.app.call_from_thread(self._on_sources_file, url, text, gen)

    def _on_sources_file(self, url: str, text: str, gen: int) -> None:
        if gen != self._probe_gen:
            return                                  # a newer probe is in flight
        self._sources_url = url
        self._apply_paste(text)

    def _apply_paste(self, raw: str) -> None:
        text = raw.strip()
        status = self.query_one("#detect-status", Label)
        if not text:
            self._reset()
            return
        entries = parse_apt_sources(text)
        if not entries:
            if sum(1 for ln in text.splitlines() if ln.strip()) >= 2:
                status.update(
                    "[dim]Not recognized — expected a sources.list line or deb822 block.[/]"
                )
                status.display = True
            self.query_one("#fields-container").display = False
            self.query_one("#bulk-row").display = False
            return

        self._probe_gen += 1
        if any(e.arch_placeholder for e in entries):
            # apt's own $(ARCH)/$ARCH substitution has no local machine to run
            # on here (see core/apt_sources.py's arch_placeholder) — resolving
            # it means probing each known architecture, which is network I/O
            # and must not run on this (UI) thread.
            status.update("[dim]Resolving $(ARCH)…[/]")
            status.display = True
            self.query_one("#fields-container").display = False
            self.query_one("#bulk-row").display = False
            self._resolve_placeholder_worker(entries, self._probe_gen)
            return

        self._display_drafts(len(entries), drafts_from_entries(entries))

    @work(thread=True, exit_on_error=False)
    def _resolve_placeholder_worker(self, entries: list[AptEntry], gen: int) -> None:
        try:
            resolved = resolve_placeholders(entries)
            drafts = drafts_from_entries(resolved)
            self.app.call_from_thread(self._on_placeholders_resolved, resolved, drafts, gen)
        except Exception as exc:
            self.app.call_from_thread(self._worker_failed, "architecture probe", exc)

    def _on_placeholders_resolved(
        self, entries: list[AptEntry], drafts: list[SourceDraft], gen: int
    ) -> None:
        if gen != self._probe_gen:
            return                                  # a newer paste is in flight
        if not drafts:
            self._draft = None
            self._bundle = []
            status = self.query_one("#detect-status", Label)
            status.update(
                "[dim]$(ARCH) resolved to no architecture — repository unreachable?[/]"
            )
            status.display = True
            self.query_one("#fields-container").display = False
            self.query_one("#bulk-row").display = False
            return
        self._display_drafts(len(entries), drafts)

    def _display_drafts(self, entry_count: int, drafts: list[SourceDraft]) -> None:
        """Show what resolved to: one repository's fields, or the bulk review
        pane for several. Shared by the ordinary paste path, the $(ARCH)
        config-paste resolution, and the $(ARCH) URL-probe resolution, so none
        of the three can drift apart from the others."""
        status = self.query_one("#detect-status", Label)
        if self._sources_url:
            remember_sources_url(drafts, self._sources_url)
        keyed = sum(1 for d in drafts if d.gpg_key)
        key_note = f"  [dim]+ {keyed} GPG key(s)[/]" if keyed else ""

        if len(drafts) > 1:
            self._draft = None
            self._bundle = drafts
            for dim, _ in _DIMENSIONS:
                self.query_one(f"#{dim}-row").display = False
            status.update(
                f"[$mmw-ok]✓ {entry_count} entries across {len(drafts)} repositories[/]"
                f"{key_note}  [dim]probing…[/]"
            )
            status.display = True
            self.query_one("#fields-container").display = False
            self.query_one("#bulk-row").display = True
            self._start_bulk_probe(drafts, self._probe_gen)
            return

        draft = drafts[0]
        status.update(f"[$mmw-ok]✓ {draft.provider.upper()} — {escape(draft.url)}[/]{key_note}")
        status.display = True
        # The config's values are a *pre-selection*: show them at once, then
        # probe so everything the repository publishes becomes visible without
        # changing what is ticked.
        self._show_draft(draft)
        if draft.provider == "apt":
            self._enrich_worker(draft, self._probe_gen)

    @work(thread=True, exit_on_error=False)
    def _enrich_worker(self, draft: SourceDraft, gen: int) -> None:
        """Widen a pasted config's dimensions to everything the repo publishes.

        The config states a filter; probing finds the full set it filtered
        *from*, so the pickers can offer the rest without changing what is
        selected — exactly what the URL path does, one step later.
        """
        try:
            draft.probe()
            self.app.call_from_thread(self._on_enriched, draft, gen)
        except Exception as exc:
            self.app.call_from_thread(self._worker_failed, "probe", exc)

    def _on_enriched(self, draft: SourceDraft, gen: int) -> None:
        if gen == self._probe_gen and draft is self._draft:
            self._refresh_dimensions()

    # ── a paste naming several repositories ──────────────────────────────────

    def _start_bulk_probe(self, drafts: list[SourceDraft], gen: int) -> None:
        """Disable Review until the probe finishes, then start it.

        `_display_drafts` used to show the Review button and start this worker
        in the same breath. `probe_all` mutates every draft from eight threads,
        and `SourceDraft.set_dimension` writes `available` then `chosen`
        non-atomically — so a pane opened mid-probe rendered pre-probe
        dimensions that never refreshed, and pressing Done could read a draft
        between those two writes and store an explicit narrow list where the
        draft meant "all", quietly pinning the repository to whatever it
        happened to hold at that instant.
        """
        self._bulk_probing = True
        try:
            btn = self.query_one("#btn-bulk", Button)
            btn.disabled = True
            btn.label = "Probing repositories…"
        except Exception:
            pass
        self._bulk_probe_worker(drafts, gen)

    @work(thread=True, exit_on_error=False)
    def _bulk_probe_worker(self, drafts: list[SourceDraft], gen: int) -> None:
        """Probe every repository in the bundle at once.

        A DGX sources tree names a dozen; probing them one after another would
        take a dozen round-trip stacks before the pane could open.
        """
        try:
            probe_all(drafts)
            self.app.call_from_thread(self._on_bulk_probed, drafts, gen)
        except Exception as exc:
            self.app.call_from_thread(self._worker_failed, "probe", exc)

    def _on_bulk_probed(self, drafts: list[SourceDraft], gen: int) -> None:
        self._bulk_probing = False
        try:
            btn = self.query_one("#btn-bulk", Button)
            btn.disabled = False
            btn.label = "Review repositories…"
        except Exception:
            pass
        if gen != self._probe_gen or drafts is not self._bundle:
            return
        ok = sum(1 for d in drafts if not d.error)
        note = "" if ok == len(drafts) else f"  [$mmw-warn]{len(drafts) - ok} could not be probed[/]"
        self.query_one("#detect-status", Label).update(
            f"[$mmw-ok]✓ {len(drafts)} repositories ready to review[/]{note}"
        )

    @on(Button.Pressed, "#btn-bulk")
    def _bulk_pressed(self, event: "Button.Pressed") -> None:
        event.stop()
        if self._bulk_probing:
            return                                  # drafts are still being written
        self._open_review()

    @work
    async def _open_review(self) -> None:
        kept = await self.app.push_screen_wait(BulkReviewPage(self._bundle, self._existing_urls))
        if kept is None:
            return                                  # cancelled: leave it alone
        self._bundle = kept
        self.query_one("#detect-status", Label).update(
            f"[$mmw-ok]✓ {len(kept)} repositor{'y' if len(kept) == 1 else 'ies'} selected[/]"
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
            text = f"{escape(', '.join(chosen))}  [dim]of {len(available)}[/]"
        else:
            text = f"{escape(', '.join(chosen[:4]))}  [dim]+{len(chosen) - 4} of {len(available)}[/]"
        self.query_one(f"#{dim}-summary", Label).update(text)

    @work
    async def _choose_dimension(self, dim: str, title: str) -> None:
        draft = self._draft
        if draft is None:
            return
        picked = await self.app.push_screen_wait(PickPage(
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
    def _suite_dimensions_worker(self, draft: SourceDraft, suite: str, gen: int) -> None:
        """Components and architectures follow from a chosen suite."""
        try:
            draft.probe_suite(suite)
            self.app.call_from_thread(self._on_suite_dimensions, draft, gen)
        except Exception as exc:
            self.app.call_from_thread(self._worker_failed, "suite probe", exc)

    def _on_suite_dimensions(self, draft: SourceDraft, gen: int) -> None:
        if gen == self._probe_gen and draft is self._draft:
            self._refresh_dimensions()

    # ── key ──────────────────────────────────────────────────────────────────

    def _refresh_key(self) -> None:
        if self._draft is not None:
            self.query_one("#field-btn-key", ActionField).set_summary(key_summary(self._draft))

    @on(Button.Pressed, "#btn-key")
    def _key_pressed(self, event: "Button.Pressed") -> None:
        event.stop()
        self._choose_key()

    @work
    async def _choose_key(self) -> None:
        draft = self._draft
        if draft is None:
            return
        result = await self.app.push_screen_wait(
            KeyPage(draft.name, draft.gpg_key_name, draft.gpg_key))
        # Only if this is still the repository on screen: a new probe while
        # the key page was open replaced it.
        if draft is self._draft and apply_key(draft, result):
            self._refresh_key()

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
                      or config.derive_name(url, suites=draft.stored("suites") or None,
                                            provider=draft.provider))
        if draft.provider == "apt" and not draft.chosen["suites"]:
            self.app.notify("Choose at least one suite.", severity="warning")
            return []
        return [draft.to_source()]
