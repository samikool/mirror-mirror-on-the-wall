"""Deployed: every repository the site serves, and the selected one's
history beside it (approved layouts A and B)."""
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Static

from mmw.core.format import fmt_size
from mmw.site import ops
from mmw.tui.ui.list_detail import ListDetail
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


def _date(stamp: str) -> str:
    """20261024T101500Z → 24 Oct"""
    import time
    try:
        return time.strftime("%d %b", time.strptime(stamp, "%Y%m%dT%H%M%SZ"))
    except ValueError:
        return stamp


class SiteHomePage(Page):
    TITLE = "Deployed"
    SCROLLS = False
    HELP = ("Every repository this site serves. Its history is on the right: every version kept\n"
            "here, the one being served marked ▶. Serving anything but the newest holds the\n"
            "repository there — bundles then store its new versions without serving them.")
    BINDINGS = [
        Binding("enter", "serve", "Serve version"),
        Binding("f", "forward", "Forward"),
        Binding("h", "hold", "Hold"),
        Binding("d", "delete", "Delete version"),
        Binding("a", "apply", "Apply bundle"),
        Binding("m", "move", "Move to root"),
        Binding("c", "certs", "Certificates"),
        Binding("t", "tidy", "Tidy", show=False),
        Binding("r", "refresh", "Refresh", show=False),
    ]

    def __init__(self):
        super().__init__()
        self._states = []
        self._history = []
        self._history_repo = None

    def compose_body(self) -> ComposeResult:
        yield Static("", id="site-banner")
        yield ListDetail(
            DataTable(cursor_type="row", zebra_stripes=True, id="site-repos"),
            _HistoryPane(), id="site-split")

    def on_mount(self) -> None:
        self.query_one("#site-banner").display = False
        t = self.query_one("#site-repos", DataTable)
        m = self.app.mset
        self._multi = m is not None and (len(m.roots) > 1 or bool(m.missing))
        t.add_columns("Repository", *(["Root"] if self._multi else []), "Serving", "Newest", "State")
        self.query_one("#site-history", DataTable).add_columns("Version", "Date", "Size", "")
        t.focus()
        self.action_refresh()

    # ── loading ──────────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self._load()

    @work(thread=True, exclusive=True, group="site-load", exit_on_error=False)
    def _load(self, done: str | None = None) -> None:
        """Reload; `done` is an action's message, said once the list is back
        (a reload landing after it would otherwise talk over it)."""
        from mmw.site.db import SiteDB
        if self.app.mset is None:
            self.app.call_from_thread(self._loaded, [], {}, "no mirror opened yet — a applies a bundle; "
                                                            "`mmw site open PATH` opens a root")
            return
        try:
            self.app.reopen()
            homes = {r.info.name: r.home for r in self.app.mset.roots}
            rows = ops.set_status(self.app.mset)
            states, newest, self._root_of = [], {}, {}
            for name, st in rows:
                states.append(st)
                self._root_of[st.repo] = name
                with SiteDB(homes[name]) as db:
                    v = db.versions(st.repo)
                    if v:
                        newest[st.repo] = v[0].number
        except Exception as e:  # noqa: BLE001 — shown on the page
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        self.app.call_from_thread(self._loaded, states, newest, done)

    def _loaded(self, states, newest, done: str | None = None) -> None:
        if not self.is_attached:
            return
        self._states = states
        t = self.query_one("#site-repos", DataTable)
        keep = self.selected_repo()
        t.clear()
        for s in states:
            serving = f"v{s.serving.number} · {_date(s.serving.stamp)}" if s.serving else "—"
            root = [self._root_of.get(s.repo, "")] if self._multi else []
            t.add_row(Text(s.label), *root, serving, f"v{newest.get(s.repo, '?')}",
                      "held" if s.held else "current", key=s.repo)
        if keep is not None and keep in {s.repo for s in states}:
            t.move_cursor(row=t.get_row_index(keep))
        if not states:
            self.set_status("nothing applied yet — a applies a bundle")
            self._show_history(None)
        else:
            if done:
                self.set_status(done, "ok")
            else:
                self.set_status(f"{len(states)} repositor{'y' if len(states) == 1 else 'ies'}")
            self._show_history(self.selected_repo())

    def selected_repo(self) -> str | None:
        t = self.query_one("#site-repos", DataTable)
        if not self._states or t.row_count == 0:
            return None
        return self._states[min(t.cursor_row, len(self._states) - 1)].repo

    @on(DataTable.RowHighlighted, "#site-repos")
    def _highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._show_history(event.row_key.value if event.row_key else None)

    def _show_history(self, repo: str | None) -> None:
        self._load_history(repo)

    @work(thread=True, exclusive=True, group="site-history", exit_on_error=False)
    def _load_history(self, repo: str | None) -> None:
        rows = []
        if repo is not None:
            try:
                rows = ops.history(self.app.home_for(repo), repo)
            except Exception as e:  # noqa: BLE001
                self.app.call_from_thread(self.set_status, str(e), "error")
                return
        self.app.call_from_thread(self._history_loaded, repo, rows)

    def _history_loaded(self, repo: str | None, rows) -> None:
        if not self.is_attached or repo != self.selected_repo():
            return
        self._history = rows
        t = self.query_one("#site-history", DataTable)
        # The same repository again (a reload): keep the row the user is on.
        same = repo == self._history_repo
        keep = t.cursor_row if same else None
        self._history_repo = repo
        t.clear()
        for v, total, frees, serving in rows:
            date = _date(v.stamp) + (f" {v.snap_label}" if v.snap_label else "")
            t.add_row(f"{'▶ ' if serving else '  '}v{v.number}", Text(date), fmt_size(total),
                      "serving" if serving else f"frees {fmt_size(frees)}", key=str(v.number))
            if serving and keep is None:
                t.move_cursor(row=t.row_count - 1)
        if keep is not None and t.row_count:
            t.move_cursor(row=min(keep, t.row_count - 1))
        state = next((s for s in self._states if s.repo == repo), None)
        note = ""
        if state and state.held and rows:
            note = (f"held at v{state.serving.number} · f forward to v{rows[0][0].number}" if state.serving
                    else f"held · nothing served · f serves v{rows[0][0].number}")
        self.query_one("#site-hold-note", Static).update(note)

    def selected_version(self) -> int | None:
        t = self.query_one("#site-history", DataTable)
        if not self._history or t.row_count == 0:
            return None
        return self._history[min(t.cursor_row, len(self._history) - 1)][0].number


    # ── acting on versions ───────────────────────────────────────────────────

    @on(DataTable.RowSelected, "#site-history")
    def _history_selected(self, _event) -> None:
        self.action_serve()

    def action_serve(self) -> None:
        repo, number = self.selected_repo(), self.selected_version()
        if repo is None or number is None:
            self.set_status("Pick a version in the history first.", "error")
            return
        try:
            p = ops.serve_preview(self.app.home_for(repo), repo, number)
        except Exception as e:  # noqa: BLE001
            self.set_status(str(e), "error")
            return
        from mmw.tui.ui.dialog import ConfirmDialog
        now = f"v{p.current.number} · {_date(p.current.stamp)}" if p.current else "nothing"
        details = (f"now serving {now}\n"
                   f"relinks {p.relinks:,} file(s) · nothing to copy · indexes switch last\n"
                   + ("" if p.target.number == ops_newest(self._history) else
                      f"{p.label} stays held at v{number} until you forward it"))
        self.app.push_screen(
            ConfirmDialog(f"Serve {p.label} v{number} ({_date(p.target.stamp)})?", "",
                          confirm_label=f"Serve v{number}", confirm_variant="primary",
                          cancel_variant="default", details=details),
            lambda ok: ok and self._act(f"{p.label} serves v{number}", ops.serve, self.app.home_for(repo), repo, number))

    def action_forward(self) -> None:
        repo = self.selected_repo()
        if repo is not None:
            self._act("moved forward", ops.forward, self.app.home_for(repo), repo)

    def action_hold(self) -> None:
        repo = self.selected_repo()
        if repo is not None:
            self._act("held — bundles store its new versions without serving them",
                      ops.hold, self.app.home_for(repo), repo)

    def action_delete(self) -> None:
        repo, number = self.selected_repo(), self.selected_version()
        if repo is None or number is None:
            self.set_status("Pick a version in the history first.", "error")
            return
        from mmw.tui.ui.dialog import ConfirmDialog
        label = next(s.label for s in self._states if s.repo == repo)
        self.app.push_screen(
            ConfirmDialog(f"Delete {label} v{number} from this site?",
                          "Its files go too, unless another kept version uses them.",
                          confirm_label="Delete"),
            lambda ok: ok and self._act(f"deleted {label} v{number}", ops.delete_version,
                                        self.app.home_for(repo), repo, number))

    @work(thread=True, exclusive=True, group="site-act", exit_on_error=False)
    def _act(self, done: str, fn, *args) -> None:
        self.app.call_from_thread(self.set_status, "working…")
        try:
            result = fn(*args)
        except Exception as e:  # noqa: BLE001 — a refusal, the lock: said on the page
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        notes = getattr(result, "tls_notes", [])
        self.app.call_from_thread(self._acted, "; ".join([done, *notes]))

    def _acted(self, done: str) -> None:
        self._load(done)

    def action_move(self) -> None:
        repo = self.selected_repo()
        if repo is None:
            return
        names = [r.info.name for r in self.app.mset.roots]
        if len(names) < 2:
            self.set_status("This mirror has one root here — a bundle's new hostnames can go to another "
                            "(mmw site apply … --root HOST=PATH).", "error")
            return
        state = next(s for s in self._states if s.repo == repo)
        current = self._root_of.get(repo, names[0])

        def chosen(root):
            if not root or root == current:
                return
            from mmw.tui.ui.dialog import ConfirmDialog
            self.app.push_screen(
                ConfirmDialog(f"Move {state.label} to {root}?",
                              f"Copies every kept version into {root}'s store first. The old copy stays "
                              "served until you tidy (t) after recreating nginx.",
                              confirm_label="Move", confirm_variant="primary", cancel_variant="default"),
                lambda ok: ok and self._act(f"moved {state.label} to {root} — recreate nginx in {current} and "
                                            f"{root}, then t tidies the old copy",
                                            ops.move_repo, self.app.mset, repo, root))
        self.app.push_screen(MoveRootPage(state.label, current, names), chosen)

    def action_certs(self, password: bytes | None = None) -> None:
        self._sign(password)

    @work(thread=True, exclusive=True, group="site-act", exit_on_error=False)
    def _sign(self, password: bytes | None) -> None:
        from mmw.core.certs import CAKeyEncrypted
        from mmw.site.certs import sign_set
        self.app.call_from_thread(self.set_status, "signing…")
        try:
            results = sign_set(self.app.mset, password=password)
        except CAKeyEncrypted:
            self.app.call_from_thread(self._ask_passphrase)
            return
        except Exception as e:  # noqa: BLE001 — no CA yet, a wrong passphrase: said on the page
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        signed = [r.root for r in results if r.action == "signed"]
        text = "; ".join(f"{r.root} {r.action}" for r in results)
        notes = [f"{r.root}: {n}" for r in results for n in r.notes]
        if notes:
            text += "; " + "; ".join(notes)
        if signed:
            text += f" — recreate nginx in {', '.join(signed)}"
        self.app.call_from_thread(self.set_status, text, "ok")

    def _ask_passphrase(self) -> None:
        self.app.push_screen(PassphrasePage(), lambda pw: pw and self.action_certs(pw.encode()))

    def action_tidy(self) -> None:
        self._act("cleared moved repositories' old copies", ops.tidy_set, self.app.mset)

    def action_apply(self) -> None:
        from mmw.tui.path_pick import PathPickPage
        from mmw.tui.site.apply_flow import recent_paths
        if self.app.mset is None:
            self.set_status("No mirror opened yet: the first apply names its roots — "
                            "mmw site apply BUNDLE --root '*'=PATH", "error")
            return
        recent = recent_paths()
        self.app.push_screen(
            PathPickPage("Apply a bundle", "a bundle .tar, or a stage directory",
                         recent[0] if recent else "", dirs_only=False, suffixes=(".tar",)),
            lambda path: path and self._plan(path))

    @work(thread=True, exclusive=True, group="site-plan", exit_on_error=False)
    def _plan(self, path: str) -> None:
        from mmw.site.apply import plan_apply, plan_set_apply
        self.app.call_from_thread(self.set_status, f"reading {path}…")
        try:
            plan = plan_apply(self.app.mset, path)
            placing = plan_set_apply(self.app.mset, path)
        except Exception as e:  # noqa: BLE001 — not a bundle, and the like
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        self.app.call_from_thread(self._review, plan, path, placing)

    def _review(self, plan, path: str, placing=None) -> None:
        from mmw.tui.site.apply_flow import ApplyReviewPage, remember
        remember(path)
        here = placing.locations if placing is not None else {}
        if placing is not None and any(o not in here for o in placing.owner.values()):
            # A new hostname needs a root, or a root is missing: place first.
            from mmw.tui.site.place import PlacePage
            self.set_status("")
            self.app.push_screen(PlacePage(placing, path, self.app.mset),
                                 lambda c: c and self.app.push_screen(
                                     ApplyReviewPage(plan, path), lambda r: self._chosen(r, c.place)))
            return
        banner = self.query_one("#site-banner", Static)
        banner.update(f"▶ {Path(path).name}: {plan.network} → {plan.created} · "
                      f"{sum(1 for r in plan.rows if not r.unchanged)} repositories change")
        banner.display = True
        self.set_status("")
        self.app.push_screen(ApplyReviewPage(plan, path), self._chosen)

    def _chosen(self, choice, place=None) -> None:
        if choice is None:
            return
        if place is not None:
            from dataclasses import replace
            choice = replace(choice, place=place)
        self.app.push_screen(_run_page(choice), lambda _r: self._load())


def _run_page(choice):
    from mmw.tui.site.apply_flow import ApplyRunPage
    return ApplyRunPage(choice)


def ops_newest(history) -> int | None:
    return history[0][0].number if history else None


class PassphrasePage(Page):
    """The CA key's passphrase, for this signing only — never kept."""

    TITLE = "CA passphrase"

    def compose_body(self) -> ComposeResult:
        from textual.widgets import Input
        yield Input(password=True, placeholder="passphrase", id="ca-pass")

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-pass-cancel", compact=True)
        yield Button("Sign", id="btn-pass-go", variant="primary", compact=True)

    @on(Button.Pressed, "#btn-pass-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, _event) -> None:
        self._go()

    @on(Button.Pressed, "#btn-pass-go")
    def _go(self) -> None:
        from textual.widgets import Input
        self.dismiss(self.query_one("#ca-pass", Input).value or None)


class MoveRootPage(Page):
    """Which root a repository should live in."""

    TITLE = "Move"

    def __init__(self, label: str, current: str, roots: list[str]):
        super().__init__()
        self.subtitle = f"{label} · now in {current}"
        self._current, self._roots = current, roots

    def compose_body(self) -> ComposeResult:
        from mmw.tui.ui.form import ChoiceField, Form
        yield Form(ChoiceField("Root", [(r, r) for r in self._roots], self._current, id="move-root",
                               hint="Every kept version's files are copied there first; nothing is served "
                                    "from it until the copy is complete."))

    def compose_actions(self) -> ComposeResult:
        from mmw.tui.ui.widgets import Button
        yield Button("Cancel", id="btn-move-cancel", compact=True)
        yield Button("Move", id="btn-move-go", variant="primary", compact=True)

    @on(Button.Pressed, "#btn-move-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-move-go")
    def _go(self) -> None:
        from textual.widgets import Select
        self.dismiss(self.query_one("#move-root", Select).value)


class _HistoryPane(Static):
    """The selected repository's versions, and its hold."""

    def compose(self) -> ComposeResult:
        yield DataTable(cursor_type="row", zebra_stripes=True, id="site-history")
        yield Static("", id="site-hold-note")
