"""The far side's settings and upkeep in the TUI, as `mmw site settings`,
`keep`, `check-signatures` and `adopted` are on the command line: what the
mirror keeps (and what each choice would free) and how far behind it serves,
a suite's own keep and lag, checking
adopted points' signatures, and the trees adopted from."""
from __future__ import annotations

from textual import on, work
from textual.app import ComposeResult
from textual.markup import escape
from textual.widgets import DataTable, Static

from mmw.core.format import fmt_size
from mmw.tui.ui.form import Form, TextField, ToggleField
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


from mmw.site.home import keep_words, lag_words  # noqa: E402 — one way to say them, here and in the CLI


def parse_keep_text(text: str):
    """What a person typed for a keep: a number, `all` or an age like `90d`."""
    from mmw.site.home import parse_keep
    text = text.strip()
    return parse_keep(int(text) if text.isdigit() else text)


class SiteSettingsPage(Page):
    """What every root of the mirror keeps, whether Clean up runs after each
    apply, and dated URLs — with what each keep would free, on request."""

    TITLE = "Settings"
    HELP = ("History per suite: a number of points, all, or an age like 90d. Clean up (C) applies\n"
            "it; with Clean after each apply on, every apply does. What each would free reads every\n"
            "kept point's files — it can take a moment on a big mirror. Lag: each suite serves its\n"
            "newest point at least that old — a test network none, production 7d.")

    def __init__(self, roots, keep, auto_clean: bool | None, dated: bool | None, lag: str | None = None):
        """`auto_clean`/`dated` None: not known here (only roots over ssh) —
        those stay as each root has them."""
        super().__init__()
        self._roots = roots
        self.subtitle = ", ".join(r.info.name for r in roots)
        self._keep, self._auto, self._dated, self._lag = keep, auto_clean, dated, lag

    def compose_body(self) -> ComposeResult:
        fields = [TextField("Keep", str(self._keep), id="set-keep",
                            hint=f"now: {keep_words(self._keep)} per suite — N, all, or an age like 90d"),
                  TextField("Lag", self._lag or "none", id="set-lag",
                            hint=f"now: {lag_words(self._lag)} — each suite serves its newest point at "
                                 "least this old (7d), or none")]
        if self._auto is not None:
            fields += [ToggleField("Clean after each apply", self._auto, id="set-auto",
                                   hint="Off: nothing is deleted until you Clean up (C)"),
                       ToggleField("Dated URLs", self._dated, id="set-dated",
                                   hint="Every kept point at HOST/PATH/@YYYY-MM-DD/")]
        yield Form(*fields)
        yield Static("" if self._auto is not None else
                     "Clean up after each apply and dated URLs: set on each root over ssh (mmw site settings there).",
                     id="set-costs")

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-set-cancel", compact=True)
        yield Button("What each would free", id="btn-set-costs", compact=True)
        yield Button("Save", id="btn-set-save", variant="primary", compact=True)

    @on(Button.Pressed, "#btn-set-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-set-costs")
    def _costs_pressed(self) -> None:
        self.query_one("#set-costs", Static).update("working out what each choice would free…")
        self._costs()

    @work(thread=True, exclusive=True, group="site-costs", exit_on_error=False)
    def _costs(self) -> None:
        lines = []
        for r in self._roots:
            try:
                costs = r.keep_costs()
            except Exception as e:  # noqa: BLE001 — said on the page
                lines.append(f"{r.info.name}: {e}")
                continue
            lines.append(f"{r.info.name}: Clean up would free, keeping — " + ", ".join(
                f"{keep_words(k)}: {fmt_size(v)}" for k, v in costs.items()))
        self.app.call_from_thread(self._show_costs, "\n".join(lines))

    def _show_costs(self, text: str) -> None:
        if self.is_attached:
            self.query_one("#set-costs", Static).update(escape(text))

    @on(Button.Pressed, "#btn-set-save")
    def _save(self) -> None:
        from mmw.site.home import parse_lag
        values = self.query_one(Form).values()
        try:
            keep = parse_keep_text(values["set-keep"])
            lag = parse_lag(values["set-lag"])
        except ValueError as e:
            self.set_status(str(e), "error")
            return
        # Only what changed goes to the roots: one left alone keeps its own.
        changed = {}
        if keep != self._keep:
            changed["keep"] = keep
        if lag != self._lag:
            changed["lag"] = lag or "none"
        if self._auto is not None and bool(values["set-auto"]) != self._auto:
            changed["auto_clean"] = bool(values["set-auto"])
        if self._dated is not None and bool(values["set-dated"]) != self._dated:
            changed["dated"] = bool(values["set-dated"])
        self.dismiss(changed)


class SuiteSettingsPage(Page):
    """One suite's own settings — what it keeps and how far behind it
    serves; each left empty: the mirror's."""

    TITLE = "Suite settings"

    def __init__(self, label: str, suite: str, keep, site_keep, lag: str | None = None,
                 site_lag: str | None = None):
        super().__init__()
        self.subtitle = f"{label} · {suite}"
        self._keep, self._site = keep, site_keep
        self._lag, self._site_lag = lag, site_lag

    def compose_body(self) -> ComposeResult:
        yield Form(TextField("Keep", "" if self._keep is None else str(self._keep), id="suite-keep",
                             placeholder=f"the mirror's: {keep_words(self._site)}",
                             hint="N, all, or an age like 90d — empty: what the mirror keeps"),
                   TextField("Lag", self._lag or "", id="suite-lag",
                             placeholder=f"the mirror's: {lag_words(self._site_lag)}",
                             hint="An age like 7d (serve the newest point at least that old), "
                                  "none — empty: the mirror's"))

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-suite-cancel", compact=True)
        yield Button("Save", id="btn-suite-save", variant="primary", compact=True)

    @on(Button.Pressed, "#btn-suite-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-suite-save")
    def _save(self) -> None:
        """Dismisses what changed: {"keep": value | "site", "lag": "Nd" | "none" | "site"}."""
        from mmw.site.home import parse_lag
        values = self.query_one(Form).values()
        keep_text, lag_text = values["suite-keep"].strip(), values["suite-lag"].strip().lower()
        try:
            keep = "site" if not keep_text else parse_keep_text(keep_text)
            lag = "site" if not lag_text else (parse_lag(lag_text) or "none")
        except ValueError as e:
            self.set_status(str(e), "error")
            return
        changed = {}
        if keep != ("site" if self._keep is None else parse_keep_text(str(self._keep))):
            changed["keep"] = keep
        if lag != (self._lag or "site"):
            changed["lag"] = lag
        self.dismiss(changed)


class CheckSignaturesPage(Page):
    """Which key checks the points adopted without one."""

    TITLE = "Check signatures"
    HELP = ("Points adopted without a key are marked 'signature not checked'. Check them with\n"
            "upstream's public key — a file — or, left empty, with the keys this mirror serves.")

    def compose_body(self) -> ComposeResult:
        yield Form(TextField("Key file", "", id="sig-key", placeholder="upstream's public key (else this mirror's keys)"))

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-sig-cancel", compact=True)
        yield Button("Check", id="btn-sig-go", variant="primary", compact=True)

    @on(Button.Pressed, "#btn-sig-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-sig-go")
    def _go(self) -> None:
        self.dismiss(self.query_one(Form).values()["sig-key"].strip())


class AdoptedPage(Page):
    """The trees this mirror adopted from (not in place): what removing each
    frees — what was adopted from it stays served."""

    TITLE = "Adopted trees"

    def __init__(self, rows: list[tuple[object, object]]):
        super().__init__()
        self._rows = rows                       # (root, Original)

    def compose_body(self) -> ComposeResult:
        t = DataTable(cursor_type="row", zebra_stripes=True, id="adopted-table")
        t.add_columns("Tree", "Adopted", "Removing it frees")
        for _root, o in self._rows:
            t.add_row(str(o.path), f"{o.stamp[:4]}-{o.stamp[4:6]}-{o.stamp[6:8]}",
                      f"{o.files:,} files · {fmt_size(o.frees)}" if o.present else "gone already")
        yield t
        yield Static("Nothing adopted from elsewhere." if not self._rows else
                     "What was adopted from a tree stays served when the tree goes.", id="adopted-note")

    def compose_actions(self) -> ComposeResult:
        yield Button("Close", id="btn-adopted-close", compact=True)
        yield Button("Remove…", id="btn-adopted-remove", variant="error", compact=True,
                     disabled=not self._rows)

    @on(Button.Pressed, "#btn-adopted-close")
    def _close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-adopted-remove")
    def _remove(self) -> None:
        t = self.query_one("#adopted-table", DataTable)
        self.dismiss(self._rows[min(t.cursor_row, len(self._rows) - 1)])
