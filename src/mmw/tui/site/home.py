"""Deployed: every repository the site serves, and the selected one's
history beside it (approved layouts A and B)."""
from pathlib import Path
import time

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Static
from textual.worker import WorkerState

from mmw.core.format import fmt_size
from mmw.site import ops
from mmw.tui.ui.list_detail import ListDetail
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


def _esc(text: str) -> str:
    """Text shown as it is inside markup — a `[` in a label or path is not a
    tag. (rich's escape leaves Textual markup's `[$var]` and a trailing `\\`.)"""
    return str(text).replace("[", "\\[")


def _bundle_when(stamp: str, stamps: list[str]) -> str:
    """A bundle as a person tells it apart: its day, the year when not this
    one, and the time when another bundle shares the day."""
    try:
        t = time.strptime(stamp, "%Y%m%dT%H%M%SZ")
    except ValueError:
        return stamp
    fmt = "%d %b" + ("" if t.tm_year == time.gmtime().tm_year else " %Y")
    if sum(s[:8] == stamp[:8] for s in stamps) > 1:
        fmt += " %H:%M"
    return time.strftime(fmt, t)


def _date(stamp: str) -> str:
    """20261024T101500Z → 24 Oct"""
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
        Binding("r", "add_root", "Add root"),
        Binding("b", "serve_bundle", "Serve bundle", show=False),
        Binding("m", "move", "Move to root"),
        Binding("c", "certs", "Certificates"),
        Binding("t", "tidy", "Tidy", show=False),
        Binding("ctrl+r", "refresh", "Refresh", show=False),
    ]

    def __init__(self):
        super().__init__()
        self._states = []
        self._root_of: dict[str, str] = {}
        self._kept: dict[str, tuple[int, int]] = {}
        self._bundles: list[tuple[str, str | None, int]] = []     # (stamp, label, repositories it changed)
        self._history = []
        self._history_repo = None

    def compose_actions(self) -> ComposeResult:
        # The mirror's own actions; a version's sit under its table (the
        # pane), so both rows fit at 80 columns. Apply is the main thing here.
        yield Button("Apply bundle…", id="btn-site-apply", variant="primary", compact=True)
        yield Button("Add root…", id="btn-site-add-root", compact=True)
        yield Button("As of…", id="btn-site-serve-bundle", compact=True)
        yield Button("Move…", id="btn-site-move", compact=True)
        yield Button("Certs", id="btn-site-certs", compact=True)
        yield Button("Tidy", id="btn-site-tidy", compact=True)

    _BUTTONS = {"btn-site-apply": "apply", "btn-site-add-root": "add_root", "btn-site-serve-bundle": "serve_bundle",
                "btn-site-serve": "serve",
                "btn-site-forward": "forward", "btn-site-hold": "hold", "btn-site-delete": "delete",
                "btn-site-move": "move", "btn-site-certs": "certs", "btn-site-tidy": "tidy"}

    def on_button_pressed(self, event) -> None:
        action = self._BUTTONS.get(event.button.id or "")
        if action:
            event.stop()
            if self.check_action(action, ()):
                getattr(self, f"action_{action}")()

    def _acting(self) -> bool:
        return any(w.group == "site-act" and w.state in (WorkerState.PENDING, WorkerState.RUNNING)
                   for w in self.workers)

    def check_action(self, action: str, parameters) -> bool | None:
        # One change to the mirror at a time: a second would cancel the first
        # in Textual's eyes while its thread carries on, and their messages race.
        if action in self._BUTTONS.values() and self._acting():
            return False
        return super().check_action(action, parameters)

    def on_worker_state_changed(self, event) -> None:
        if event.worker.group == "site-act":
            busy = self._acting()
            for button_id in self._BUTTONS:
                self.query_one(f"#{button_id}", Button).disabled = busy
            self.refresh_bindings()

    def action_add_root(self) -> None:
        if self.app.mset is None:
            self.set_status("No mirror opened yet — the first apply makes its roots.", "error")
            return
        from mmw.tui.path_pick import PathPickPage
        self.app.push_screen(PathPickPage("Add a root", "a folder (an empty or new one becomes a new root), "
                                          "or type host:/path for one over ssh", remote=True, new=True),
                             lambda path: path and self.add_root_path(path))

    def add_root_path(self, path: str) -> None:
        self.set_status(f"Adding root {path}…")
        self._act_root(path)

    @work(thread=True, exclusive=True, group="site-act", exit_on_error=False)
    def _act_root(self, path: str) -> None:
        try:
            msg = ops.add_root(self.app.mset, path)
        except Exception as e:  # noqa: BLE001 — another mirror's root, ssh refused: said on the page
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        self.app.call_from_thread(self._acted, msg)

    def compose_body(self) -> ComposeResult:
        yield Static("", id="site-banner")
        yield ListDetail(
            DataTable(cursor_type="row", zebra_stripes=True, id="site-repos"),
            _HistoryPane(), id="site-split")

    def _is_root(self) -> bool:
        return True                     # the far side's home: nothing to go back to (q quits)

    def on_mount(self) -> None:
        self.query_one("#site-banner").display = False
        t = self.query_one("#site-repos", DataTable)
        t.show_header = False
        t.add_column("Repository")
        self.query_one("#site-history", DataTable).add_columns("Version", "Date", "Size", "")
        t.focus()
        self.action_refresh()

    # ── loading ──────────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self._load()

    @work(thread=True, exclusive=True, group="site-load", exit_on_error=False)
    def _load(self, done: str | None = None, kind: str = "ok") -> None:
        """Reload; `done` is an action's message, said once the list is back
        (a reload landing after it would otherwise talk over it)."""
        if self.app.mset is None:
            self.app.call_from_thread(self._loaded, [], {}, "no mirror opened yet — a applies a bundle; "
                                                            "`mmw site open PATH` opens a root")
            return
        try:
            self.app.reopen()
            roots_by = {r.info.name: r for r in self.app.mset.roots}
            rows = ops.set_status(self.app.mset)
            # Built here, kept on the page only by _loaded: a load that fails
            # part-way leaves the last good one in place.
            states, newest, root_of, kept, bundles = [], {}, {}, {}, {}
            for name, st in rows:
                states.append(st)
                root_of[st.repo] = name
                history = roots_by[name].history(st.repo)
                if history:
                    newest[st.repo] = history[0][0].number
                # For the card: how many versions are kept here, and their size.
                kept[st.repo] = (len(history), max((total for _v, total, _f, _s in history), default=0))
                for v, *_ in history:            # every bundle still kept here, for Serve bundle…
                    _stamp, label, count = bundles.get(v.stamp, (v.stamp, v.snap_label, 0))
                    bundles[v.stamp] = (v.stamp, label, count + 1)
        except Exception as e:  # noqa: BLE001 — shown on the page
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        self.app.call_from_thread(self._loaded, states, newest, done, root_of, kept,
                                  sorted(bundles.values(), reverse=True), kind)

    def _loaded(self, states, newest, done: str | None = None, root_of=None, kept=None, bundles=None,
                kind: str = "ok") -> None:
        if not self.is_attached:
            return
        self._states = states
        self._root_of, self._kept, self._bundles = root_of or {}, kept or {}, bundles or []
        t = self.query_one("#site-repos", DataTable)
        keep = self.selected_repo()
        t.clear()
        colour = self.app.get_css_variables()   # a table cell is Rich: the theme's colours, resolved
        for s in states:
            # A card: what it is; what it serves against the newest kept; where it lives.
            serving = f"▶ v{s.serving.number} · {_date(s.serving.stamp)}" if s.serving else "nothing served"
            hosts = ", ".join(sorted({b.split("/", 1)[0] for b in s.bases}))
            kept, size = self._kept.get(s.repo, (0, 0))
            card = Text(s.label, style="bold")
            card.append(f"\n{serving}   newest v{newest.get(s.repo, '?')}   ")
            card.append("held" if s.held else "current", style=colour["mmw-warn" if s.held else "mmw-ok"])
            card.append(f"\n{hosts} · {self._root_of.get(s.repo, '')} · {fmt_size(size)} · "
                        f"{kept} version{'s' if kept != 1 else ''}", style="dim")
            t.add_row(card, key=s.repo, height=3)
        if keep is not None and keep in {s.repo for s in states}:
            t.move_cursor(row=t.get_row_index(keep))
        if not states:
            self.set_status("nothing applied yet — a applies a bundle")
            self._show_history(None)
        else:
            if done:
                self.set_status(done, kind)
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
        rows, details = [], {}
        if repo is not None:
            try:
                root = self.app.root_of(repo)
                rows = root.history(repo)
            except Exception as e:  # noqa: BLE001
                self.app.call_from_thread(self.set_status, str(e), "error")
                return
            details = {"root": root.info.name, "location": root.info.location}
            try:
                details.update(root.details())
            except Exception:  # noqa: BLE001 — an older mmw over ssh has no `details`: the rest still shows
                pass
        self.app.call_from_thread(self._history_loaded, repo, rows, details)

    def _detail_text(self, repo: str | None, rows, details: dict) -> str:
        """The selected repository, in full: where, what it serves, what's kept."""
        state = next((s for s in self._states if s.repo == repo), None)
        if state is None:
            return ""
        newest = rows[0][0] if rows else None
        serving = (f"v{state.serving.number} · {_date(state.serving.stamp)} · from bundle {state.serving.stamp}"
                   if state.serving else "nothing")
        held = "   [$mmw-warn]held[/]" if state.held and not newest else ""
        if state.held and newest:
            if not state.serving:
                held = f"   [$mmw-warn]held · nothing served — Forward serves v{newest.number}[/]"
            elif newest.number != state.serving.number:
                held = f"   [$mmw-warn]held at v{state.serving.number} — Forward serves v{newest.number}[/]"
            else:
                held = f"   [$mmw-warn]held at v{state.serving.number}[/]"
        total = max((t for _v, t, _f, _s in rows), default=0)
        frees = sum(f for _v, _t, f, on in rows if not on)
        hosts = ", ".join(sorted({b.split("/", 1)[0] for b in state.bases}))
        lines = [
            f"[b]{_esc(state.label)}[/]",
            f"[dim]root[/]       {_esc(details.get('root', ''))}  [dim]({_esc(details.get('location', ''))})[/]",
            f"[dim]hostname[/]   {_esc(hosts)}",
            f"[dim]serving[/]    {_esc(serving)}",
            "[dim]newest[/]     " + (f"v{newest.number} · {_date(newest.stamp)}" if newest else "—")
            + ("  [$mmw-ok]current[/]" if not state.held else "") + held,
            f"[dim]kept[/]       {len(rows)} version{'s' if len(rows) != 1 else ''} · {fmt_size(total)}"
            + (f" · old ones free {fmt_size(frees)}" if frees else ""),
            f"[dim]https[/]      {_esc(details.get('https', '—'))}",
            "[dim]last apply[/] " + (f"{_date(details['last_apply'])} · {_esc(details.get('last_apply_kind', ''))}"
                                     if details.get("last_apply") else "none yet"),
        ]
        return "\n".join(lines)

    def _history_loaded(self, repo: str | None, rows, details: dict | None = None) -> None:
        if not self.is_attached or repo != self.selected_repo():
            return
        self.query_one("#site-detail", Static).update(self._detail_text(repo, rows, details or {}))
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
            p = self.app.root_of(repo).serve_preview(repo, number)
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
            lambda ok: ok and self._act(f"{p.label} serves v{number}", self.app.root_of(repo).serve, repo, number))

    def action_forward(self) -> None:
        repo = self.selected_repo()
        if repo is not None:
            self._act("moved forward", self.app.root_of(repo).forward, repo)

    def action_hold(self) -> None:
        repo = self.selected_repo()
        if repo is not None:
            self._act("held — bundles store its new versions without serving them",
                      self.app.root_of(repo).hold, repo)

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
            lambda ok: ok and self._act(f"deleted {label} v{number}", self.app.root_of(repo).delete,
                                        repo, number))

    @work(thread=True, exclusive=True, group="site-act", exit_on_error=False)
    def _act(self, done: str, fn, *args) -> None:
        self.app.call_from_thread(self.set_status, "working…")
        try:
            result = fn(*args)
        except Exception as e:  # noqa: BLE001 — a refusal, the lock: said on the page
            # Reloaded all the same: part of it may have happened (serving
            # several repositories, one root's lock held part-way).
            self.app.call_from_thread(self._load, str(e), "error")
            return
        notes = getattr(result, "tls_notes", [])
        self.app.call_from_thread(self._acted, "; ".join([done, *notes]))

    def _acted(self, done: str) -> None:
        self._load(done)

    def action_serve_bundle(self) -> None:
        if not self._bundles:
            self.set_status("No bundle applied here yet.", "error")
            return
        self.app.push_screen(ServeBundlePage(self._bundles), lambda stamp: stamp and self._bundle_plan(stamp))

    @work(thread=True, exclusive=True, group="site-bundle-plan", exit_on_error=False)
    def _bundle_plan(self, stamp: str) -> None:
        self.app.call_from_thread(self.set_status, "reading every repository's versions…")
        try:
            steps = ops.bundle_plan(self.app.mset, stamp)
        except Exception as e:  # noqa: BLE001 — an ssh root down: said on the page
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        self.app.call_from_thread(self._confirm_bundle, stamp, steps)

    def _confirm_bundle(self, stamp: str, steps) -> None:
        from mmw.tui.ui.dialog import ConfirmDialog
        when = _bundle_when(stamp, [b[0] for b in self._bundles])
        if not any(s.changes for s in steps):
            self.set_status(f"Every repository already serves what it had after the {when} bundle.", "ok")
            return
        self.set_status("")
        self.app.push_screen(
            ConfirmDialog(f"Serve the mirror as of the {when} bundle?",
                          "Each repository serves its newest version from then; those that came later stay "
                          "as they are. Any not on its newest is held until you forward it.",
                          confirm_label="Serve", confirm_variant="primary", cancel_variant="default",
                          details="\n".join(s.line() for s in steps)),
            lambda ok: ok and self._act(f"serving the mirror as of the {when} bundle", ops.serve_bundle, steps))

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
            try:
                size = fmt_size(ops.move_size(self.app.mset, repo, root))
            except Exception as e:  # noqa: BLE001
                self.set_status(str(e), "error")
                return
            self.app.push_screen(
                ConfirmDialog(f"Move {state.label} to {root}?",
                              f"Copies {size} (every kept version) into {root}'s store first. The old copy stays "
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
            lambda path: path and self.app.offer_far_update(path, lambda: self._plan(path)))

    def resume_bundle(self, path: str) -> None:
        self._plan(path)                           # just updated: carry on with the same bundle

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


class ServeBundlePage(Page):
    """Which bundle to serve the whole mirror as of."""

    TITLE = "Serve bundle"

    def __init__(self, bundles: list[tuple[str, str | None, int]]):
        super().__init__()
        self.subtitle = "every repository at its newest version from then"
        self._bundles = bundles

    def compose_body(self) -> ComposeResult:
        from mmw.tui.ui.form import ChoiceField, Form
        stamps = [b[0] for b in self._bundles]
        options = [(" · ".join([_bundle_when(stamp, stamps), *([label] if label else []),
                                f"{n} repositor{'y' if n == 1 else 'ies'}"]), stamp)
                   for stamp, label, n in self._bundles]
        yield Form(ChoiceField("Bundle", options, self._bundles[0][0], id="bundle-pick",
                               hint="A repository the bundle didn't change serves its version from before it."))

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-bundle-cancel", compact=True)
        yield Button("Review…", id="btn-bundle-go", variant="primary", compact=True)

    @on(Button.Pressed, "#btn-bundle-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-bundle-go")
    def _go(self) -> None:
        from textual.widgets import Select
        self.dismiss(self.query_one("#bundle-pick", Select).value)


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
    """The selected repository in full, its versions, and its hold."""

    def compose(self) -> ComposeResult:
        yield Static("", id="site-detail")
        yield DataTable(cursor_type="row", zebra_stripes=True, id="site-history")
        with Horizontal(id="site-version-actions"):
            yield Button("Serve", id="btn-site-serve", compact=True)
            yield Button("Forward", id="btn-site-forward", compact=True)
            yield Button("Hold", id="btn-site-hold", compact=True)
            yield Button("Delete", id="btn-site-delete", variant="error", compact=True)
