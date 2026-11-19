"""Publish and transfer selectors over retained repository points.

Pool reads run off the event loop. The shared command layer resolves endpoint
choices for both the TUI and CLI without creating network snapshots.
"""
from pathlib import Path

from textual import on, work
from textual.app import ComposeResult
from textual.markup import escape
from textual.widgets import Input, Label, Select, SelectionList
from textual.widgets.selection_list import Selection

from mmw.core import config
from mmw.core.format import fmt_size
from mmw.core.snapshots import SnapshotStatus
from mmw.tui.publish_flows import BundleChoice, PublishChoice
from mmw.tui.ui.form import ChoiceField, Form, PathField, TextField, ToggleField
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button, JumpSelectionList


class PublishPickPage(Page):
    """Which repository point date to publish, and into which directory.
    Dismisses with a `PublishChoice`, or None."""

    def __init__(self, net: config.NetworkConfig, status: SnapshotStatus, available_dates=()):
        super().__init__()
        self.TITLE = f"Publish {net.name}"
        self._net = net
        self._status = status
        self._available_dates = available_dates

    def compose_body(self) -> ComposeResult:
        targets = [("Latest repository points", "live"),
                   ("A date — repository points at or before then", "date")]
        from mmw.site.versionref import day
        targets += [(f"{day(stamp)} {stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]} UTC — retained point",
                     f"point:{stamp}") for stamp in self._available_dates]
        dirs = ([(escape(f"The network's root — {self._net.root}"), "root")]
                if self._net.root is not None else []) + [("Another directory", "other")]
        yield Form(
            ChoiceField("Publish", targets, "live", id="pick-target",
                        hint="Use this network's current repository selection at the chosen date."),
            TextField("Date", "", id="pick-when",
                      hint="2026-11-03, 3nov or a stamp — each repository as its last sync "
                           "by then had it (--as-of)."),
            ChoiceField("Into", dirs, dirs[0][1], id="pick-dir",
                        hint="Another directory gets its own nginx config, same hostnames — "
                             "serve it on its own address."),
            PathField("Directory", "", id="pick-root", browse_title="Publish into",
                      hint="Created if missing."),
            ToggleField("Recheck every file", False, id="publish-recheck",
                        hint="Ignore the record of the last publish (--recheck) — slow; for a "
                             "tree edited by hand."),
            id="pick-form")
        serving = self._status.serving
        if serving is not None:
            yield Label(f"[dim]The root serves {escape(self._status.name(serving))} now.[/]",
                        id="pick-note")

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-pick-cancel", compact=True)
        yield Button("Publish", id="btn-pick-go", variant="primary", compact=True)

    def on_mount(self) -> None:
        self._show_root()
        self._show_when()
        self.query_one("#btn-pick-go").focus()

    @on(Select.Changed, "#pick-target")
    def _show_when(self) -> None:
        self.query_one("#field-pick-when").display = (
            self.query_one("#pick-target", Select).value == "date")

    @on(Select.Changed, "#pick-dir")
    def _show_root(self) -> None:
        self.query_one("#field-pick-root").display = (
            self.query_one("#pick-dir", Select).value == "other")

    @on(Button.Pressed, "#btn-pick-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-pick-go")
    def _go(self) -> None:
        values = self.query_one(Form).values()
        root = None
        if values["pick-dir"] == "other":
            if not values["pick-root"]:
                self.set_status("Choose the directory to publish into.", "error")
                self.query_one("#pick-root").focus()
                return
            root = Path(values["pick-root"]).expanduser()
        target = values["pick-target"]
        as_of = None
        if target.startswith('point:'):
            as_of, target = target.removeprefix('point:'), 'live'
        if target == "date":
            from mmw.core.points import resolve_when
            try:
                resolve_when(values["pick-when"] or "")
            except ValueError as e:
                self.set_status(f"Date: {e}", "error")
                self.query_one("#pick-when").focus()
                return
            as_of, target = values["pick-when"].strip(), "live"
        self.dismiss(PublishChoice(root=root,
                                   recheck=values["publish-recheck"],
                                   as_of=as_of))


class BundlePickPage(Page):
    """Transfer retained points: the assumed baseline (From) and points to carry (To),
    with what that sends, as a bundle or (mmw) a stage. Dismisses with a `BundleChoice`, or None."""

    def __init__(self, net: config.NetworkConfig, status: SnapshotStatus, mirror_dir: Path,
                 repos: dict[str, str] | None = None, installer_due: bool | None = None,
                 available_dates: tuple[str, ...] = ()):
        super().__init__()
        # Worked out by the caller off the screen's thread (it reads the pool);
        # None: here, as a page built on its own (a test) has no caller to ask.
        self._due = installer_due
        self.TITLE = f"Transfer {net.name}"
        # An mmw far side: key → label of the repositories to choose from.
        self._repos = repos
        self._net = net
        self._status = status
        self._mirror_dir = mirror_dir
        # Where `bundle_network` starts by default: the last bundle's
        # destination, or before any, the newest imported bundle manifest.
        last = status.last_bundle.to_stamp if status.last_bundle else next(
            (s.stamp for s in status.snapshots if s.kind == "imported"), None)
        self._last = last
        # What "Include mmw installer" adds to the estimate (mmw far side).
        self._installer_note = "mmw installer"
        self._preview_generation = 0
        self._preview_timer = None
        self._available_dates = available_dates

    def compose_body(self) -> ComposeResult:
        froms = ([(escape(f"Where the last transfer went — {self._status.name(self._last)}"), "last")]
                 if self._last else [])
        froms += [("Nothing — send everything", "none"),
                  ("A date — what the far side had then", "date")]
        tos = [("Latest recorded points", "now"), ("A date — newest point by then, per suite", "date")]
        from mmw.site.versionref import day
        dates = [(f"{day(stamp)} {stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]} UTC — retained point",
                  f"point:{stamp}") for stamp in self._available_dates]
        froms += dates
        tos += dates
        yield Form(
            ChoiceField("From", froms, froms[0][1], id="pick-from",
                        hint="Last transfer uses each repository's local record; arrival across the airgap is unknown."),
            TextField("Date", "", id="pick-since",
                      hint="YYYY-MM-DD (end of day UTC) or YYYYMMDDTHHMMSSZ."),
            PathField("Kept bundles", str(self._net.bundle_dir or ""), id="pick-kept",
                      browse_title="Kept bundles", hint="Search here as well as the network's bundle folder and ledger."),
            ChoiceField("To", tos, "now", id="pick-to",
                        hint="Points to carry. Older points are stored; rollback is an explicit site action."),
            TextField("Through date", "", id="pick-until",
                      hint="Newest retained point at or before this date, independently per suite."),
            *([ChoiceField("How", [("One .tar file (bundle)", "bundle"),
                                   ("A folder of hardlinks (stage)", "stage")],
                           "bundle", id="pick-as",
                           hint="A folder costs almost no disk; tar, rsync or copy it across yourself.")]
              if self._repos else []),
            ChoiceField("Points", [("Each suite's newest", "newest"),
                                   ("All retained points in the range", "history")],
                        "history" if self._net.history else "newest", id="pick-points",
                        hint="Every point gives the far side each sync's state, for old builds; "
                             "the newest is smaller."),
            ChoiceField("mmw installer", [("Include mmw installer", "yes"), ("Not this time", "no")],
                        "yes" if self._installer_due() else "no", id="pick-installer",
                        hint="Written beside it: the far side installs mmw from it, offline."),
            id="pick-form")
        if self._repos:
            yield Label("[dim]Repositories · space toggles · ctrl+a toggles all[/]")
            yield JumpSelectionList(*[Selection(label, key, True) for key, label in self._repos.items()],
                                    id="pick-repos", compact=True)
        yield Label("", id="pick-estimate")

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-pick-cancel", compact=True)
        yield Button("Transfer", id="btn-pick-go", variant="primary", compact=True)

    def on_mount(self) -> None:
        self._show_since()
        self._show_until()
        self._estimate_now()
        self.query_one("#btn-pick-go").focus()
        self._installer_size()

    @work(thread=True, exclusive=True, exit_on_error=False, group="bundle-installer")
    def _installer_size(self) -> None:
        from mmw.core.runtime import installer_note
        note = installer_note(self._net, self._mirror_dir)
        self.app.call_from_thread(self._got_installer, note)

    def _got_installer(self, note: str) -> None:
        if not self.is_mounted:
            return
        self._installer_note = note
        self._estimate_now()

    def _installer_due(self) -> bool:
        if self._due is not None:
            return self._due
        from mmw.core import runtime as rt
        try:
            return rt.installer_due(self._net, self._mirror_dir)
        except Exception:  # noqa: BLE001 — a default, never a failure
            return not self._last

    def _installer(self) -> bool:
        return self.query_one(Form).values().get("pick-installer") == "yes"

    def _say(self, text: str, start: str | None) -> None:
        """The estimate line, plus mmw's installer when it is included."""
        self.query_one("#pick-estimate", Label).update(
            text + (f"\n[dim]+ {escape(self._installer_note)}[/]" if self._installer() else ""))

    def _ends(self) -> tuple[str, str]:
        values = self.query_one(Form).values()
        return values["pick-from"], values["pick-to"]

    def _picked_repos(self) -> tuple[str, ...] | None:
        """The ticked repositories, or None when all are (or there is no list)."""
        if not self._repos:
            return None
        picked = tuple(k for k in self._repos if k in self.query_one("#pick-repos", SelectionList).selected)
        return None if len(picked) == len(self._repos) else picked

    @on(Select.Changed, "#pick-from")
    def _show_since(self) -> None:
        show = self.query_one("#pick-from", Select).value == "date"
        self.query_one("#field-pick-since").display = show
        self.query_one("#field-pick-kept").display = show

    @on(Select.Changed, "#pick-to")
    def _show_until(self) -> None:
        self.query_one("#field-pick-until").display = self._ends()[1] == "date"

    def _range_options(self):
        values = self.query_one(Form).values()
        start, dest = self._ends()
        return dict(since=self._endpoint(start, values["pick-since"]),
                    until=self._endpoint(dest, values["pick-until"]),
                    full=start == "none", history=values["pick-points"] == "history",
                    repos=self._picked_repos(),
                    kept_dirs=[Path(values["pick-kept"]).expanduser()] if values["pick-kept"] else None)

    @staticmethod
    def _endpoint(selected, custom):
        return selected.removeprefix("point:") if selected.startswith("point:") else (
            custom.strip() if selected == "date" else None)

    @on(Input.Changed, "#pick-since")
    @on(Input.Changed, "#pick-until")
    @on(Input.Changed, "#pick-kept")
    @on(SelectionList.SelectedChanged, "#pick-repos")
    @on(Select.Changed)
    def _estimate_now(self) -> None:
        self._preview_generation += 1
        gen = self._preview_generation
        if self._preview_timer is not None:
            self._preview_timer.stop()
        start, dest = self._ends()
        options = self._range_options()
        if start == "date" and not options["since"]:
            self._say("[dim]Enter the date the far side holds.[/]", start)
            return
        if dest == "date" and not options["until"]:
            self._say("[dim]Enter the end date to see each suite's retained point.[/]", start)
            return
        self._say("[dim]Reading retained points…[/]", start)
        self._preview_timer = self.set_timer(.15, lambda: self._preview_range(gen, options))

    @work(thread=True, exclusive=True, exit_on_error=False, group="bundle-range-preview")
    def _preview_range(self, gen, options) -> None:
        from mmw.commands.bundle import preview_transfer
        from mmw.site.versionref import day
        try:
            plan = preview_transfer(self._net, self._mirror_dir, cfg=getattr(self.app, 'cfg', None), **options)
            source = f"since {plan.source}" if options["since"] else plan.source
            if plan.source.startswith("the points"):
                source += " (no bundle that old was found)"
            lines = [source]
            for row in plan.rows:
                label = (self._repos or {}).get(row.repo, row.repo)
                available = (f"; kept {day(row.available[0])} to {day(row.available[-1])}"
                             if row.available else "")
                lines.append(f"{label} / {row.suite}: {day(row.from_date) if row.from_date else 'full'}"
                             f" → {day(row.to_date)} · {row.points} point(s){available}")
            lines.append(f"Sends about {plan.blobs:,} new blobs · {fmt_size(plan.bytes)}")
            lines.extend(plan.warnings)
            text = escape("\n".join(lines))
        except Exception as exc:
            text = escape(str(exc) or type(exc).__name__)
        self.app.call_from_thread(self._range_previewed, gen, text)

    def _range_previewed(self, gen, text) -> None:
        if gen == self._preview_generation and self.is_mounted:
            self._say(text, self._ends()[0])

    @on(Button.Pressed, "#btn-pick-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-pick-go")
    def _go(self) -> None:
        start, dest = self._ends()
        from mmw.core.points import resolve_when
        for selected, field in ((start, "pick-since"), (dest, "pick-until")):
            if selected != "date":
                continue
            try:
                resolve_when(self.query_one(Form).values()[field])
            except ValueError as exc:
                self.set_status(f"Date: {exc}", "error")
                self.query_one(f"#{field}", Input).focus()
                return
        if self._picked_repos() == ():
            self.set_status("Tick at least one repository.", "error")
            return
        from mmw.core.transfer_ranges import validate_dates
        try:
            validate_dates(self._range_options()['since'], self._range_options()['until'])
        except ValueError as exc:
            self.set_status(str(exc), 'error')
            return
        self.dismiss(self.choice())

    def choice(self) -> BundleChoice:
        """What is chosen now."""
        start, dest = self._ends()
        values = self.query_one(Form).values()
        return BundleChoice(from_last=start in ("last", "date") or start.startswith("point:"),
                            since=self._endpoint(start, values["pick-since"]),
                            kept_dir=Path(values["pick-kept"]).expanduser() if values["pick-kept"] else None,
                            until=self._endpoint(dest, values["pick-until"]),
                            repos=self._picked_repos(),
                            stage=bool(self._repos) and values["pick-as"] == "stage",
                            installer=self._installer(),
                            history=(values["pick-points"] == "history") if "pick-points" in values else None)
