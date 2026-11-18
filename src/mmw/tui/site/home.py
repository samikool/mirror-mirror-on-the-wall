"""Repositories: every repository the site serves, and the selected one's
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
from mmw.tui.site.run_page import RunPage
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


def _esc(text: str) -> str:
    """Text shown as it is inside markup — a `[` in a label or path is not a
    tag. (rich's escape leaves Textual markup's `[$var]` and a trailing `\\`.)"""
    return str(text).replace("[", "\\[")


def and_list(items: list[str]) -> str:
    """a · a and b · a, b and c"""
    items = list(items)
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def where(location: str) -> str:
    """Where a root is, as a person needs it: a root over ssh says which
    machine it's on; a local one, its path."""
    from mmw.site.ssh import parse_location
    kind, host, path = parse_location(location)
    return f"on {host} · {path}" if kind == "ssh" else location


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
    """20261024T101500Z → 24 Oct (with its year when not this one: an
    upstream Release can be years old)"""
    try:
        t = time.strptime(stamp, "%Y%m%dT%H%M%SZ")
    except ValueError:
        return stamp
    return time.strftime("%d %b" + ("" if t.tm_year == time.gmtime().tm_year else " %Y"), t)


class SiteHomePage(Page):
    TITLE = "Repositories"
    SCROLLS = False
    HELP = ("Every repository this site serves. Its history is on the right, by suite: every\n"
            "point kept here (one upstream Release, by its date), the one served marked ▶.\n"
            "Serving a point pins its suite there — bundles then store newer ones without\n"
            "serving them, until you forward it.\n"
            "k: local .debs this site adds to the repository, and how it is then signed;\n"
            "K: this site's signing keys (carried ones from the connected side included);\n"
            "u: bring in the mmw instances this site serves locally (mmw site local);\n"
            "s: what the mirror keeps (and what each choice frees); e: a suite's own keep;\n"
            "g: check adopted points' signatures; o: trees adopted from; v: verify (and repair).")
    BINDINGS = [
        # Short, so the footer fits 80 columns; the buttons say the rest.
        Binding("enter", "serve", "Serve", show=False),
        Binding("f", "forward", "Forward"),
        Binding("h", "hold", "Hold"),
        Binding("d", "delete", "Delete"),
        Binding("a", "apply", "Apply"),
        Binding("A", "adopt", "Adopt", show=False),
        Binding("C", "clean_up", "Clean up", show=False),
        Binding("r", "add_root", "Add root"),
        Binding("b", "serve_bundle", "As of", show=False),
        Binding("x", "export", "Export"),
        Binding("m", "move", "Move"),
        Binding("c", "certs", "Certs", show=False),         # its button is in the bar
        Binding("t", "tidy", "Tidy", show=False),
        Binding("k", "packages", "Packages", show=False),
        Binding("K", "signing_keys", "Signing keys", show=False),
        Binding("u", "local_update", "Update local", show=False),
        Binding("j", "combine", "Combine", show=False),
        Binding("s", "settings", "Settings", show=False),
        Binding("e", "suite_keep", "Suite keep", show=False),
        Binding("g", "check_signatures", "Check signatures", show=False),
        Binding("o", "adopted", "Adopted trees", show=False),
        Binding("v", "verify", "Verify", show=False),
        Binding("ctrl+r", "refresh", "Refresh", show=False),
    ]

    def __init__(self):
        super().__init__()
        self._states = []
        self._root_of: dict[str, str] = {}
        self._kept: dict[str, tuple[int, int]] = {}
        self._bundles: list[tuple[str, str | None, int]] = []     # (stamp, label, repositories it changed)
        self._last_apply: tuple[str, str] | None = None           # the mirror's: (stamp, kind), any root
        self._said = 0                                             # messages shown so far (see _loaded)
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
        from mmw.site.ssh import parse_location
        from mmw.tui.path_pick import PathPickPage
        local = [r.info.location for r in self.app.mset.roots if parse_location(r.info.location)[0] == "local"]
        start = str(Path(local[0]).parent) if local else ""          # beside the roots, not wherever mmw ran
        self.app.push_screen(PathPickPage("Add a root", "a folder (an empty or new one becomes a new root), "
                                          "or type host:/path for one over ssh", start, remote=True, new=True),
                             lambda path: path and self.add_root_path(path))

    def add_root_path(self, path: str) -> None:
        self.set_status(f"Adding root {path}…")
        self._act_root(path)

    @work(thread=True, exclusive=True, group="site-act", exit_on_error=False)
    def _act_root(self, path: str) -> None:
        from mmw.site.ssh import RemoteMmw
        try:
            msg = ops.add_root(self.app.mset, path)
        except RemoteMmw as e:
            if e.installable:              # no mmw there, or an older one: offer to install it, then add again
                from mmw.tui.site.install_flow import offer_install
                self.app.call_from_thread(offer_install, self, path, e, lambda: self.add_root_path(path),
                                          self.set_status)
            else:
                self.app.call_from_thread(self.set_status, str(e), "error")
            return
        except Exception as e:  # noqa: BLE001 — another mirror's root, ssh refused: said on the page
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        self.app.call_from_thread(self._acted, msg)

    def compose_body(self) -> ComposeResult:
        yield Static("", id="site-banner")
        yield ListDetail(
            DataTable(cursor_type="row", zebra_stripes=True, id="site-repos"),
            _HistoryPane(), id="site-split")

    def set_status(self, message: str, kind: str = "info") -> None:
        self._said += 1
        super().set_status(message, kind)

    def _is_root(self) -> bool:
        return True                     # the far side's home: nothing to go back to (q quits)

    def on_mount(self) -> None:
        self.query_one("#site-banner").display = False
        t = self.query_one("#site-repos", DataTable)
        t.show_header = False
        t.add_column("Repository")
        self.query_one("#site-history", DataTable).add_columns("Point", "Date", "Size", "")
        t.focus()
        self.action_refresh()

    # ── loading ──────────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self._load()

    @work(thread=True, exclusive=True, group="site-load", exit_on_error=False)
    def _load(self, done: str | None = None, kind: str = "ok") -> None:
        """Reload; `done` is an action's message, said once the list is back
        (a reload landing after it would otherwise talk over it)."""
        said = self._said                        # a message after this one outranks the load's summary
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
            last = None                          # the mirror's last apply, whichever root it landed in
            for r in self.app.mset.roots:
                try:
                    d = r.details()
                except Exception:  # noqa: BLE001 — an older mmw over ssh: it just isn't counted
                    continue
                if d.get("last_apply") and (last is None or d["last_apply"] > last[0]):
                    last = (d["last_apply"], d.get("last_apply_kind") or "")
            for name, st in rows:
                states.append(st)
                root_of[st.repo] = name
                history = roots_by[name].history(st.repo)
                newest[st.repo] = _summary(history)
                points = [(p, total) for _t, rows in history for p, total, _f, mark in rows if mark != "stub"]
                # For the card: how many points are kept here, and the biggest.
                kept[st.repo] = (len(points), max((total for _p, total in points), default=0))
                for p, _total in points:         # every bundle still kept here, for Serve bundle…
                    _stamp, label, count = bundles.get(p.stamp, (p.stamp, p.snap_label, 0))
                    bundles[p.stamp] = (p.stamp, label, count + 1)
            from mmw.commands.site import conversion_notices
            notices = conversion_notices(self.app.mset)
        except Exception as e:  # noqa: BLE001 — shown on the page
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        self._last_apply = last
        self.app.call_from_thread(self._loaded, states, newest, done, root_of, kept,
                                  sorted(bundles.values(), reverse=True), kind, said)
        if notices:
            self.app.call_from_thread(self._converted, notices)

    def _converted(self, notices) -> None:
        if not self.is_attached:
            return
        from mmw.tui.ui.dialog import ConfirmDialog
        self.app.push_screen(ConfirmDialog(
            "Converted to timelines", _esc("\n".join(notices)),
            confirm_label="OK", cancel_label="Close", confirm_variant="primary"))

    def _loaded(self, states, newest, done: str | None = None, root_of=None, kept=None, bundles=None,
                kind: str = "ok", said: int | None = None) -> None:
        if not self.is_attached:
            return
        self._states = states
        self._root_of, self._kept, self._bundles = root_of or {}, kept or {}, bundles or []
        t = self.query_one("#site-repos", DataTable)
        keep = self.selected_repo()
        t.clear()
        colour = self.app.get_css_variables()   # a table cell is Rich: the theme's colours, resolved
        for s in states:
            # A card, short enough for a 35-column list: what it serves and
            # whether that's its newest; then where it lives and what's kept.
            # (Its hostnames are in the detail beside it.)
            info = newest.get(s.repo) or {}
            served = info.get("served") or []
            serving = f"▶ {_date(max(served))}" if served else "nothing served"
            kept, size = self._kept.get(s.repo, (0, 0))
            card = Text(s.label, style="bold")
            card.append(f"\n{serving} · ")
            state = "held" if s.held else "pinned" if info.get("pinned") else "current"
            card.append(state, style=colour["mmw-ok" if state == "current" else "mmw-warn"])
            if info.get("newer"):
                card.append(" · newer stored")
            if info.get("incomplete"):
                card.append(" · incomplete")
            card.append(f"\n{self._root_of.get(s.repo, '')} · {kept} point{'s' if kept != 1 else ''} · "
                        f"{fmt_size(size)}", style="dim")
            t.add_row(card, key=s.repo, height=3)
        if keep is not None and keep in {s.repo for s in states}:
            t.move_cursor(row=t.get_row_index(keep))
        if not states:
            self.set_status("nothing applied yet — a applies a bundle")
            self._show_history(None)
        else:
            if done:
                self.set_status(done, kind)
            elif said is None or said == self._said:   # nothing said since the load began
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
            layer_details = getattr(root, "layer_details", None)       # a local root's only
            if layer_details is not None:
                details.update(layer_details(repo))
        self.app.call_from_thread(self._history_loaded, repo, rows, details)

    def _detail_text(self, repo: str | None, rows, details: dict) -> str:
        """The selected repository, in full: where, what it serves, what's kept."""
        state = next((s for s in self._states if s.repo == repo), None)
        if state is None:
            return ""
        info = _summary(rows)
        served = []
        for t, points in rows:
            for p, _total, _frees, mark in points:
                if mark == "serving":
                    served.append(f"{ops.suite_of(t)} {_date(p.date)}" + (" (pinned)" if t.pinned is not None else ""))
        serving = ", ".join(served) or "nothing"
        flags = []
        if state.held:
            flags.append("[$mmw-warn]held — bundles are stored, not served; Forward moves it on[/]")
        elif info["pinned"]:
            flags.append("[$mmw-warn]pinned — Forward serves each suite's newest[/]")
        elif info["newer"]:
            flags.append("[$mmw-warn]a newer point is stored but not served[/]")
        else:
            flags.append("[$mmw-ok]current[/]")
        if info["incomplete"]:
            flags.append("[$mmw-warn]a point is incomplete: bring its files with a full bundle[/]")
        held = " · ".join(flags)
        kept = [(p, t_, f) for _t, points in rows for p, t_, f, mark in points if mark != "stub"]
        total = max((t_ for _p, t_, _f in kept), default=0)
        frees = sum(f for _t, points in rows for _p, _t2, f, mark in points if mark not in ("serving", "stub"))
        hosts = ", ".join(sorted({b.split("/", 1)[0] for b in state.bases}))
        lines = [
            f"[b]{_esc(state.label)}[/]",
            f"[dim]root[/]       {_esc(details.get('root', ''))}  [dim]({_esc(where(details.get('location', '')))})[/]",
            f"[dim]hostname[/]   {_esc(hosts)}",
            f"[dim]serving[/]    {_esc(serving)}"
            + (f"  [$mmw-ok]+ local packages ({_esc(details['local'])})[/]" if details.get("local") else ""),
            *([f"[$mmw-warn]held by its local packages: {_esc(details['layer_hold'])}[/]"]
              if details.get("layer_hold") else []),
            "[dim]state[/]      " + held,
            f"[dim]kept[/]       {len(kept)} point{'s' if len(kept) != 1 else ''} · {fmt_size(total)}"
            + (f" · old ones free {fmt_size(frees)}" if frees else ""),
            f"[dim]https[/]      {_esc(details.get('https', '—'))}",
            # The mirror's, not this root's: a root a repository moved into
            # may never have had a bundle applied to it.
            "[dim]last apply[/] " + (f"{_date(self._last_apply[0])} · {_esc(self._last_apply[1])}"
                                     if self._last_apply else "none yet"),
        ]
        return "\n".join(lines)

    def _history_loaded(self, repo: str | None, rows, details: dict | None = None) -> None:
        if not self.is_attached or repo != self.selected_repo():
            return
        self.query_one("#site-detail", Static).update(self._detail_text(repo, rows, details or {}))
        # One row per suite heading, then its points: (track, point or None, total, frees, mark).
        flat = []
        for t, points in rows:
            flat.append((t, None, 0, 0, ""))
            flat += [(t, p, total, frees, mark) for p, total, frees, mark in points]
        self._history = flat
        t = self.query_one("#site-history", DataTable)
        # The same repository again (a reload): keep the row the user is on.
        same = repo == self._history_repo
        keep = t.cursor_row if same else None
        self._history_repo = repo
        t.clear()
        for track, p, total, frees, mark in flat:
            if p is None:
                t.add_row(Text(ops.suite_of(track), style="bold"), "", "", "", key=f"t{track.id}")
                continue
            from mmw.site.versionref import short
            date = _date(p.date) + (f" {p.snap_label}" if p.snap_label else "")
            tail = mark or f"frees {fmt_size(frees)}"
            t.add_row(f"{'▶ ' if mark == 'serving' else '  '}{short(p.ident)}", Text(date),
                      fmt_size(total) if mark != "stub" else "—", tail, key=f"p{p.id}")
            if mark == "serving" and keep is None:
                t.move_cursor(row=t.row_count - 1)
        if keep is not None and t.row_count:
            t.move_cursor(row=min(keep, t.row_count - 1))

    def selected_point(self):
        """(track, point, mark) at the history's cursor; None on a suite heading."""
        t = self.query_one("#site-history", DataTable)
        if not self._history or t.row_count == 0:
            return None
        track, p, _total, _frees, mark = self._history[min(t.cursor_row, len(self._history) - 1)]
        return (track, p, mark) if p is not None else None

    # ── acting on versions ───────────────────────────────────────────────────

    @on(DataTable.RowSelected, "#site-history")
    def _history_selected(self, _event) -> None:
        self.action_serve()

    def action_serve(self) -> None:
        repo, picked = self.selected_repo(), self.selected_point()
        if repo is None or picked is None:
            self.set_status("Pick a point in the history first.", "error")
            return
        track, point, mark = picked
        if mark == "stub":
            self.set_status("That point was deleted here — only its record is kept.", "error")
            return
        self.set_status(f"working out what serving {ops.suite_of(track)} {_date(point.date)} changes…")
        self._serve_preview(repo, track, point)

    @work(thread=True, exclusive=True, group="site-preview", exit_on_error=False)
    def _serve_preview(self, repo: str, track, point) -> None:
        from mmw.site.versionref import name_of
        try:                                     # over ssh, a round trip: never on the screen's thread
            p = self.app.root_of(repo).serve_preview(repo, name_of(point), ops.suite_of(track))
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        self.app.call_from_thread(self._confirm_serve, repo, track, point, p)

    def _confirm_serve(self, repo: str, track, point, p) -> None:
        from mmw.tui.ui.dialog import ConfirmDialog
        self.set_status("")
        suite, when = ops.suite_of(track), _date(point.date)
        details = (f"now serving {', '.join(p.current) or 'nothing'}\n"
                   f"relinks {p.relinks:,} file(s) · nothing to copy · indexes switch last\n"
                   f"{p.label} {suite} stays pinned at {when} until you forward it")
        current = {}
        for t, rows in _rows_of(self._history):
            for q, _total, _frees, mark in rows:
                if mark == "serving":
                    current[ops.suite_of(t)] = (q.date, q.ident)
        step = self._step(repo, current, {suite: (point.date, point.ident)})
        self.app.push_screen(
            ConfirmDialog(f"Serve {p.label} {suite} as of {when}?", "",
                          confirm_label="Serve", confirm_variant="primary",
                          cancel_variant="default", details=details),
            lambda ok: ok and self._switch([step], "Serving", p.relinks))

    def _step(self, repo: str, current: dict, target: dict, *, forward: bool = False):
        """One repository to switch, for the progress page."""
        state = next(s for s in self._states if s.repo == repo)
        return ops.BundleStep(self.app.root_of(repo), repo, state.label, current, target, forward=forward)

    def _switch(self, steps, title: str, total: int | None = None) -> None:
        self.app.push_screen(SwitchRunPage(steps, title, total), lambda _: self._load())

    def action_forward(self) -> None:
        repo = self.selected_repo()
        if repo is not None:
            self._forward_plan(repo)

    @work(thread=True, exclusive=True, group="site-preview", exit_on_error=False)
    def _forward_plan(self, repo: str) -> None:
        try:                                     # from the root itself (ssh: off the screen)
            rows = self.app.root_of(repo).history(repo)
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        self.app.call_from_thread(self._forward, repo, rows)

    def _forward(self, repo: str, rows) -> None:
        state = next((s for s in self._states if s.repo == repo), None)
        if state is None:
            return
        info = _summary(rows)
        if not (state.held or info["pinned"] or info["newer"]):
            self.set_status(f"{state.label} already serves each suite's newest")
            return
        if not info["newer"]:                    # nothing to switch: only the hold or pins to clear
            self._act(f"{state.label}: {'hold' if state.held else 'pins'} cleared — already at its newest",
                      self.app.root_of(repo).forward, repo)
            return
        self._switch([self._step(repo, {}, {}, forward=True)], "Forwarding")

    def action_hold(self) -> None:
        repo = self.selected_repo()
        if repo is not None:
            self._act("held — bundles store its new points without serving them",
                      self.app.root_of(repo).hold, repo)

    def action_delete(self) -> None:
        repo, picked = self.selected_repo(), self.selected_point()
        if repo is None or picked is None:
            self.set_status("Pick a point in the history first.", "error")
            return
        track, point, mark = picked
        if mark == "stub":
            self.set_status("That point is already deleted here — only its record is kept.", "error")
            return
        from mmw.site.versionref import name_of
        from mmw.tui.ui.dialog import ConfirmDialog
        label = next(s.label for s in self._states if s.repo == repo)
        suite, when = ops.suite_of(track), _date(point.date)
        root = self.app.root_of(repo)
        args = (repo, name_of(point), suite)
        if mark == "serving":
            warn = (f"It is being served: {suite} then serves the next newest point kept here. "
                    "Its record stays in the history.")
            self.app.push_screen(
                ConfirmDialog(f"Delete {label} {suite} {when}, which is being served?", warn,
                              confirm_label="Delete anyway", confirm_variant="warning"),
                lambda ok: ok and self._run_action(f"Deleting {label} {suite} {when}", [(repo, label)],
                                                   lambda progress: root.delete(*args, True, progress=progress),
                                                   lambda freed: f"deleted {label} {suite} {when} — "
                                                                 f"{fmt_size(freed)} freed"))
            return
        self.app.push_screen(
            ConfirmDialog(f"Delete {label} {suite} {when} from this site?",
                          "Its files go too, unless another kept point uses them. Its record stays.",
                          confirm_label="Delete"),
            lambda ok: ok and self._run_action(f"Deleting {label} {suite} {when}", [(repo, label)],
                                               lambda progress: root.delete(*args, progress=progress),
                                               lambda freed: f"deleted {label} {suite} {when} — "
                                                             f"{fmt_size(freed)} freed"))

    def _run_action(self, title: str, rows, job, said) -> None:
        """A change that may take a while, on its own progress page."""
        self.app.push_screen(ActionRunPage(title, rows, job, said), lambda _: self._load())

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

    def action_export(self) -> None:
        """Write a bundle of this mirror's history for another network."""
        if self.app.mset is None or not self._states:
            self.set_status("Nothing to export yet — apply a bundle first.", "error")
            return
        self._export_targets()

    @work(thread=True, exclusive=True, group="site-export", exit_on_error=False)
    def _export_targets(self) -> None:
        targets: list[str] = []
        try:                                     # over ssh, a round trip: never on the screen's thread
            for root in self.app.mset.roots:
                targets += [t for t in root.export_targets() if t not in targets]
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        self.app.call_from_thread(self._export_page, targets)

    def _export_page(self, targets: list[str]) -> None:
        from mmw.tui.site.export_flow import ExportPage, ExportRunPage
        page = ExportPage(self.app.mset.network, targets, [(s.repo, s.label) for s in self._states])
        self.app.push_screen(page, lambda c: c and self.app.push_screen(ExportRunPage(c), lambda _: self._load()))

    def action_serve_bundle(self) -> None:
        if not self._bundles:
            self.set_status("No bundle applied here yet.", "error")
            return
        self.app.push_screen(ServeBundlePage(self._bundles), lambda stamp: stamp and self._bundle_plan(stamp))

    @work(thread=True, exclusive=True, group="site-bundle-plan", exit_on_error=False)
    def _bundle_plan(self, stamp: str) -> None:
        self.app.call_from_thread(self.set_status, "reading every repository's points…")
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
                          "Each suite serves its newest point from then; repositories that came later stay "
                          "as they are. Each stays pinned there until you forward it.",
                          confirm_label="Serve", confirm_variant="primary", cancel_variant="default",
                          details="\n".join(s.line() for s in steps)),
            lambda ok: ok and self._switch(steps, f"As of the {when} bundle"))

    def action_move(self) -> None:
        repo = self.selected_repo()
        if repo is None:
            return
        names = [r.info.name for r in self.app.mset.roots]
        place = {r.info.name: where(r.info.location) for r in self.app.mset.roots}
        if len(names) < 2:
            self.set_status("This mirror has one root here — a bundle's new hostnames can go to another "
                            "(mmw site apply … --root HOST=PATH).", "error")
            return
        state = next(s for s in self._states if s.repo == repo)
        current = self._root_of.get(repo, names[0])

        def chosen(root):
            if not root or root == current:
                return
            self.set_status(f"working out what moving {state.label} to {root} takes…")
            self._move_preview(repo, root, lambda p: confirm(root, p))

        def confirm(root, p):
            from mmw.tui.ui.dialog import ConfirmDialog
            self.set_status("")
            size, hosts = fmt_size(p.copies), and_list(p.hosts)
            if p.whole_host:
                # A hostname is served from one root: moving one of its
                # repositories means moving the host, and everything on it.
                title = f"Move the whole host {hosts} to {root} ({place[root]})?"
                message = (f"{hosts} is served from one root, so {state.label} can't move on its own: "
                           f"{and_list(p.labels)} move together. Copies {size} (every kept version of each) into "
                           f"{root}'s store first; they switch together once it's all there. {current} keeps "
                           f"serving them until nginx is recreated in both roots, then tidy (t) clears the old copy.")
            else:
                title = f"Move {state.label} to {root} ({place[root]})?"
                message = (f"Copies {size} (every kept version) into {root}'s store first. {current} keeps "
                           "serving it until nginx is recreated in both roots, then tidy (t) clears the old copy.")
            self.app.push_screen(
                ConfirmDialog(title, message, confirm_label="Move", confirm_variant="primary",
                              cancel_variant="default"),
                lambda ok: ok and self.app.push_screen(MoveRunPage(repo, current, root, p), lambda _: self._load()))
        self.app.push_screen(MoveRootPage(state.label, current,
                                          [(r.info.name, r.info.location) for r in self.app.mset.roots]), chosen)

    @work(thread=True, exclusive=True, group="site-preview", exit_on_error=False)
    def _move_preview(self, repo: str, root: str, then) -> None:
        try:                                     # lists every kept file: never on the screen's thread
            p = ops.move_preview(self.app.mset, repo, root)
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        self.app.call_from_thread(then, p)

    def action_certs(self, password: bytes | None = None) -> None:
        from mmw.site.certs import show_ca
        if password is None and show_ca() is None:
            # No CA yet: set one up here, rather than send the person to the CLI.
            self.app.push_screen(CaSetupPage(), self._ca_chosen)
            return
        self._sign(password)

    def _ca_chosen(self, how: str | None) -> None:
        from mmw.tui.path_pick import PathPickPage
        if how == "make":
            self.app.push_screen(
                PathPickPage("Where the new CA goes", "a folder for ca.crt and ca.key (a new one is made) — "
                             "keep it off the mirror", new=True),
                lambda path: path and self.app.push_screen(
                    PassphrasePage("Protect the new CA's key", "Make CA", optional=True),
                    lambda pw: pw is not None and self.make_ca(path, f"{self.app.mset.network} mmw CA",
                                                               pw.encode() or None)))
        elif how == "load":
            self.app.push_screen(
                PathPickPage("The CA's certificate", "its .crt (or .pem)", dirs_only=False, suffixes=(".crt", ".pem")),
                lambda crt: crt and self.app.push_screen(
                    PathPickPage("The CA's key", "its .key (or .pem)", str(Path(crt).parent),
                                 dirs_only=False, suffixes=(".key", ".pem")),
                    lambda key: key and self.load_ca(crt, key, None)))

    @work(thread=True, exclusive=True, group="site-act", exit_on_error=False)
    def make_ca(self, where: str, name: str, password: bytes | None) -> None:
        from mmw.site import certs as C
        self.app.call_from_thread(self.set_status, "making the CA…")
        try:
            crt, key = C.make_ca(Path(where), name, password)
            C.load_ca(crt, key, password)
        except Exception as e:  # noqa: BLE001 — a CA already there, a folder it can't write
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        self.app.call_from_thread(self.set_status, f"made the CA at {where} — now signing…")
        self._sign_now(password)

    @work(thread=True, exclusive=True, group="site-act", exit_on_error=False)
    def load_ca(self, crt: str, key: str, password: bytes | None) -> None:
        from mmw.core.certs import CAKeyEncrypted
        from mmw.site import certs as C
        try:
            C.load_ca(Path(crt), Path(key), password)
        except CAKeyEncrypted:
            self.app.call_from_thread(lambda: self.app.push_screen(
                PassphrasePage("The CA key's passphrase", "Load"),
                lambda pw: pw and self.load_ca(crt, key, pw.encode())))
            return
        except Exception as e:  # noqa: BLE001 — not a CA, not its key
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        self._sign_now(password)

    def _sign_now(self, password: bytes | None) -> None:
        """From a worker that just set up the CA: sign with it."""
        self.app.call_from_thread(self._sign, password)

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

    def _local_root(self, repo: str | None = None):
        """A root on this machine (the repository's, or any): local packages
        and keys need its files; an ssh root has none here."""
        roots = [self.app.root_of(repo)] if repo is not None else list(self.app.mset.roots)
        root = next((r for r in roots if hasattr(r, "packages_target")), None)
        if root is None:
            self.set_status("That is on a root reached over ssh — run mmw site there for this.", "warning")
        return root

    def action_packages(self) -> None:
        repo = self.selected_repo()
        root = self._local_root(repo) if repo is not None else None
        if root is None:
            return
        from mmw.tui.site.packages import SitePackagesPage
        home, store = root.packages_target()
        self.app.push_screen(SitePackagesPage(home, store, repo), lambda _: self._load())

    def action_local_update(self) -> None:
        root = self._local_root()
        if root is not None:
            self._local_update(root)

    @work(thread=True, exclusive=True, group="site-local", exit_on_error=False)
    def _local_update(self, root) -> None:
        self.app.call_from_thread(self.set_status, "Reading the local instances…")
        try:
            results = root.local_update()
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        if not results:
            text, kind = "No local instances — add one with: mmw site local add PATH -n NETWORK", "warning"
        else:
            text = "  ".join(f"{r.network}: " + (r.error or (f"{len(r.updated)} updated" if r.updated
                                                           else "unchanged")) for r in results)
            kind = "error" if any(r.error for r in results) else "ok"
        self.app.call_from_thread(self.set_status, text, kind)
        self.app.call_from_thread(self._load)

    def action_signing_keys(self) -> None:
        # The selected repository's root: a site layer signs with its own root's keyring.
        root = self._local_root(self.selected_repo())
        if root is None:
            return
        from mmw.tui.signing_keys import SigningKeysPage
        self.app.push_screen(SigningKeysPage(root.keyring_root(), root.key_users))

    def action_combine(self) -> None:
        """Serve slices from different days of one suite together (re-signed)."""
        repo, picked = self.selected_repo(), self.selected_point()
        if repo is None or picked is None:
            self.set_status("Pick a point of the suite in the history first.", "error")
            return
        track, point, _mark = picked
        newest = next((p for t, p, *_x in self._history if t.id == track.id and p is not None), None)
        from mmw.site.versionref import day
        days = sorted({day(p.date) for p in (point, newest) if p is not None})
        keys = []
        root = self.app.root_of(repo)
        if hasattr(root, "keyring_root"):              # an ssh root's keys are there: typed by name
            from mmw.core.signing import Keyring
            keys = [k.name for k in Keyring(root.keyring_root()).list()]
        label = next(s.label for s in self._states if s.repo == repo)
        self.app.push_screen(
            CombinePage(label, ops.suite_of(track), days, keys),
            lambda c: c and self._run_action(
                f"Combining {label} {c[0]}", [(repo, label)],
                lambda progress: self.app.root_of(repo).combine(repo, c[0], c[1], c[2], progress=progress),
                lambda r: f"combined {label} {c[0]} from {', '.join(r.sources)} — re-signed with {c[2]}; "
                          f"clients get its key at {r.key_url}"))

    # ── settings and upkeep ──────────────────────────────────────────────────

    def action_settings(self) -> None:
        """What the mirror keeps, Clean up after each apply, dated URLs."""
        if self.app.mset is None:
            return
        self._settings_read(list(self.app.mset.roots))

    @work(thread=True, exclusive=True, group="site-settings", exit_on_error=False)
    def _settings_read(self, roots) -> None:
        try:
            keep = roots[0].keep_setting()
            local = next((r for r in roots if hasattr(r, "site_settings")), None)
            s = local.site_settings() if local is not None else None
        except Exception as e:  # noqa: BLE001 — said on the page
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        auto, dated = (s.auto_clean, s.dated) if s is not None else (None, None)   # ssh only: not known here
        from mmw.tui.site.settings_flow import SiteSettingsPage
        self.app.call_from_thread(self.app.push_screen, SiteSettingsPage(roots, keep, auto, dated),
                                  lambda c: self._settings_save(roots, c) if c else
                                  (c == {} and self.set_status("Nothing changed.")))

    @work(thread=True, exclusive=True, group="site-settings", exit_on_error=False)
    def _settings_save(self, roots, c: dict) -> None:
        try:
            for r in roots:
                r.set_options(keep=c.get("keep"), auto_clean=c.get("auto_clean"), dated=c.get("dated"))
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self._load, str(e), "error")
            return
        from mmw.site.home import keep_words
        said = ([f"keeps {keep_words(c['keep'])} per suite"] if "keep" in c else []) + \
               ([f"cleans {'after each apply' if c['auto_clean'] else 'only when asked'}"]
                if "auto_clean" in c else []) + \
               ([f"dated URLs {'on' if c['dated'] else 'off'}"] if "dated" in c else [])
        self.app.call_from_thread(self._load, " · ".join(said))

    def action_suite_keep(self) -> None:
        """The selected suite's own history setting."""
        repo, picked = self.selected_repo(), self.selected_point()
        if repo is None or picked is None:
            self.set_status("Pick a point of the suite in the history first.", "error")
            return
        track = picked[0]
        if not track.name:
            self.set_status("Those are files no Release names — they have no suite to keep by.", "error")
            return
        self._suite_keep_read(repo, track)

    @work(thread=True, exclusive=True, group="site-suite-keep", exit_on_error=False)
    def _suite_keep_read(self, repo: str, track) -> None:
        """The mirror's keep, from the root (over ssh, a round trip: never on the screen's thread)."""
        from mmw.site.home import keep_words
        from mmw.tui.site.settings_flow import SuiteKeepPage
        root = self.app.root_of(repo)
        try:
            site_keep = root.keep_setting()
        except Exception as e:  # noqa: BLE001 — an ssh host out of reach: said
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        label = next(s.label for s in self._states if s.repo == repo)
        suite = ops.suite_of(track)

        def chosen(c) -> None:
            if not c:
                return
            keep = None if c[0] == "site" else c[0]
            said = "what the mirror keeps" if keep is None else keep_words(keep)
            # By the track's own name: two suites never mistaken for each other.
            self._act(f"{label} {suite} keeps {said}", root.set_track_keep, repo, track.name, keep)
        self.app.call_from_thread(self.app.push_screen, SuiteKeepPage(label, suite, track.keep, site_keep), chosen)

    def action_check_signatures(self) -> None:
        roots = [r for r in (self.app.mset.roots if self.app.mset else []) if r.is_local]
        if not roots:
            self.set_status("Checking signatures runs where the files are: mmw site check-signatures there.",
                            "warning")
            return
        from mmw.tui.site.settings_flow import CheckSignaturesPage
        self.app.push_screen(CheckSignaturesPage(), lambda key: key is not None and self._run_action(
            "Checking signatures", [(r.info.name, r.info.name) for r in roots],
            lambda progress: _recheck(roots, key, progress), _rechecked))

    def action_adopted(self) -> None:
        roots = [r for r in (self.app.mset.roots if self.app.mset else []) if r.is_local]
        if not roots:
            self.set_status("Adopted trees are listed where the files are: mmw site adopted there.", "warning")
            return
        self._adopted_read(roots)

    @work(thread=True, exclusive=True, group="site-adopted", exit_on_error=False)
    def _adopted_read(self, roots) -> None:
        try:
            rows = [(r, o) for r in roots for o in r.originals()]
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        from mmw.tui.site.settings_flow import AdoptedPage
        self.app.call_from_thread(self.app.push_screen, AdoptedPage(rows), self._adopted_remove)

    def _adopted_remove(self, picked) -> None:
        if not picked:
            return
        from mmw.tui.ui.dialog import ConfirmDialog
        root, o = picked
        every = [Path(r.info.path) for r in self.app.mset.roots if r.is_local]
        refused = (f"\nNot adopted from it (refused): {', '.join(o.refused)} — removing it loses those files."
                   if o.refused else "")
        self.app.push_screen(
            ConfirmDialog(f"Remove {o.path}?", f"It frees {fmt_size(o.frees)}; what was adopted from it stays "
                                               f"served.{refused}", confirm_label="Remove", confirm_variant="error"),
            lambda ok: ok and self._run_action(
                f"Removing {o.path}", [(str(o.path), str(o.path))],
                lambda progress: (progress("phase", text=f"removing {o.path}"),
                                  root.remove_original(o.path, every))[1],
                lambda freed: f"removed {o.path} — {fmt_size(freed)} freed"))

    def action_verify(self) -> None:
        """Every served file present and what the site says it serves."""
        if self.app.mset is None:
            return
        roots = list(self.app.mset.roots)
        found: list[str] = []                    # filled root by root: a later root's failure keeps these

        def job(progress):
            _verify(roots, progress, found)
            return found
        self.app.push_screen(
            ActionRunPage("Verifying", [(r.info.name, r.info.name) for r in roots], job, _verified),
            lambda _: self._offer_repair(roots, len(found)))

    def _offer_repair(self, roots, problems: int) -> None:
        if not problems:
            self._load()
            return
        from mmw.tui.ui.dialog import ConfirmDialog
        self.app.push_screen(
            ConfirmDialog(f"Repair {problems} served file(s)?",
                          "Each is put back from the store, as the site says it serves it.",
                          confirm_label="Repair"),
            lambda ok: ok and self._run_action(
                "Repairing", [(r.info.name, r.info.name) for r in roots],
                lambda progress: sum(r.verify(repair=True) for r in roots),
                lambda fixed: f"repaired {fixed} file(s)"))

    def action_tidy(self) -> None:
        self.app.push_screen(TidyRunPage([r.info.name for r in self.app.mset.roots]), lambda _: self._load())

    def action_clean_up(self) -> None:
        """Clean up: the points past each suite's history setting."""
        roots = [r for r in (self.app.mset.roots if self.app.mset else []) if hasattr(r, "clean_plan")]
        if not roots:
            self.set_status("Nothing to clean up here.", "error")
            return
        self._clean_plan(roots)

    @work(thread=True, exclusive=True, group="site-clean", exit_on_error=False)
    def _clean_plan(self, roots) -> None:
        try:
            plans = [(r, r.clean_plan()) for r in roots]
        except Exception as e:  # noqa: BLE001 — said on the page
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        self.app.call_from_thread(self._confirm_clean, [(r, p) for r, p in plans if p])

    def _confirm_clean(self, plans) -> None:
        from textual.markup import escape

        from mmw.core.format import fmt_size
        from mmw.tui.ui.dialog import ConfirmDialog
        if not plans:
            self.set_status("Nothing to clean up — every point is kept by the setting, or served, "
                            "pinned or served before.")
            return
        items = [i for _r, plan in plans for i in plan]
        lines = [f"{escape(i.label)}  {escape(i.suite)} {i.date} — frees {fmt_size(i.frees)}" for i in items[:12]]
        if len(items) > 12:
            lines.append(f"… and {len(items) - 12} more")
        self.app.push_screen(
            ConfirmDialog(f"Clean up {len(items)} point(s)?",
                          "\n".join(lines) + "\nTheir records stay in the history; their files go unless "
                                              "another kept point uses them.",
                          confirm_label="Clean up"),
            lambda ok: ok and self._run_action(
                "Cleaning up", sorted({(i.repo, i.label) for i in items}),
                lambda progress: sum(r.clean({i.point for i in p}, progress=progress) for r, p in plans),
                lambda freed: f"cleaned up {len(items)} point(s) — {fmt_size(freed)} freed"))

    def action_adopt(self) -> None:
        """Take over APT trees mmw didn't make (or a tree apply.py manages)."""
        from mmw.tui.site.adopt_flow import AdoptPage, AdoptRunPage
        every = list(self.app.mset.roots if self.app.mset else [])
        roots = [r for r in every if r.is_local] or every      # one over ssh adopts there, over ssh
        if not roots:
            self.set_status("Adopt into a mirror — open or apply one first "
                            "(or convert an apply.py tree in place: mmw site adopt PATH).", "error")
            return
        root = str(roots[0].info.location)
        self.app.push_screen(AdoptPage(root), lambda c: c and self.app.push_screen(
            AdoptRunPage(root, c), lambda _: self._load()))

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
    def _plan(self, path: str, deploy: frozenset | None = None) -> None:
        from dataclasses import replace

        from mmw.site.apply import plan_apply, plan_set_apply
        self.app.call_from_thread(self.set_status, f"reading {path}…")
        try:
            plan = plan_apply(self.app.mset, path)
            placing = plan_set_apply(self.app.mset, path, deploy=set(deploy or ()))
        except Exception as e:  # noqa: BLE001 — not a bundle, and the like
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        if placing.foreign:
            if deploy is None and placing.not_here:
                self.app.call_from_thread(self._ask_deploy, path, placing)
                return
            kept = {r for repos in placing.hosts.values() for r in repos}
            plan = replace(plan, rows=[r for r in plan.rows if r.repo in kept])
        self.app.call_from_thread(self._review, plan, path, placing, deploy or frozenset())

    def _ask_deploy(self, path: str, placing) -> None:
        from mmw.tui.site.apply_flow import DeployPage
        self.set_status("")
        self.app.push_screen(DeployPage(placing.network, placing.not_here),
                             lambda chosen: chosen is not None and self._plan(path, chosen))

    def _review(self, plan, path: str, placing=None, deploy: frozenset = frozenset()) -> None:
        from mmw.tui.site.apply_flow import ApplyReviewPage, remember
        remember(path)
        self._deploy = deploy
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
        from dataclasses import replace
        if place is not None:
            choice = replace(choice, place=place)
        if getattr(self, "_deploy", None):
            choice = replace(choice, deploy=self._deploy)
        self.app.push_screen(_run_page(choice), lambda _r: self._load())


def _run_page(choice):
    from mmw.tui.site.apply_flow import ApplyRunPage
    return ApplyRunPage(choice)


def _summary(history) -> dict:
    """From a repository's history (track, rows): the dates it serves,
    whether a suite is pinned, a newer point is stored but not served, or a
    point is incomplete."""
    served, pinned, newer, incomplete = [], False, False, False
    for t, rows in history:
        pinned = pinned or t.pinned is not None
        for i, (p, _total, _frees, mark) in enumerate(rows):
            incomplete = incomplete or mark == "incomplete"
            if mark == "serving":
                served.append(p.date)
                newer = newer or any(m in ("", "previous") for *_x, m in rows[:i])
    return {"served": served, "pinned": pinned, "newer": newer, "incomplete": incomplete}


def _rows_of(flat) -> list:
    """The history pane's flat rows back into (track, rows)."""
    out: dict = {}
    for track, p, total, frees, mark in flat:
        rows = out.setdefault(track.id, (track, []))[1]
        if p is not None:
            rows.append((p, total, frees, mark))
    return list(out.values())


class PassphrasePage(Page):
    """The CA key's passphrase, for this signing only — never kept. With
    `optional` (protecting a new CA), an empty one means none."""

    TITLE = "CA passphrase"

    def __init__(self, title: str = "CA passphrase", go: str = "Sign", optional: bool = False):
        super().__init__()
        self.TITLE, self._go_label, self._optional = title, go, optional
        if optional:
            self.subtitle = "leave it empty for none — the key file is then all it takes to sign"

    def compose_body(self) -> ComposeResult:
        from textual.widgets import Input
        yield Input(password=True, placeholder="passphrase", id="ca-pass")

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-pass-cancel", compact=True)
        yield Button(self._go_label, id="btn-pass-go", variant="primary", compact=True)

    @on(Button.Pressed, "#btn-pass-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    def on_input_submitted(self, _event) -> None:
        self._go()

    @on(Button.Pressed, "#btn-pass-go")
    def _go(self) -> None:
        from textual.widgets import Input
        value = self.query_one("#ca-pass", Input).value
        self.dismiss(value if self._optional else (value or None))


class CombinePage(Page):
    """Which days of one suite to serve together, and the site key that signs
    the Release mmw writes for them — said before anything is done."""

    TITLE = "Combine slices"

    def __init__(self, label: str, suite: str, days: list[str], keys: list[str]):
        super().__init__()
        self.subtitle = f"{label} · {suite}"
        self._suite, self._days, self._keys = suite, days, keys

    def compose_body(self) -> ComposeResult:
        from mmw.tui.ui.form import Form, TextField
        yield Static("Each architecture and component comes from the newest of these days that has it. "
                     "The result is no longer signed by upstream: mmw writes its Release and signs it with "
                     "the key below, served beside the repository — clients must trust that key.",
                     id="combine-warning")
        yield Form(
            TextField("Days", " ".join(self._days), id="combine-days",
                      hint="Two or more, by date: 2026-11-01 2026-11-02"),
            TextField("Site key", self._keys[0] if self._keys else "", id="combine-key",
                      hint="A signing key here (Signing keys, K)" if self._keys else
                      "Its name: a signing key on the root that serves this repository"))

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-combine-cancel", compact=True)
        yield Button("Combine and re-sign", id="btn-combine-go", variant="warning", compact=True)

    @on(Button.Pressed, "#btn-combine-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-combine-go")
    def _go(self) -> None:
        from mmw.tui.ui.form import Form
        values = self.query_one(Form).values()
        days = values["combine-days"].replace(",", " ").split()
        key = values["combine-key"].strip()
        if len(days) < 2:
            self.set_status("Name two or more days of the suite.", "error")
            return
        if not key:
            self.set_status("Say which site key signs it.", "error")
            return
        self.dismiss((self._suite, days, key))


class ServeBundlePage(Page):
    """Which bundle to serve the whole mirror as of."""

    TITLE = "As of a bundle"

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


def _recheck(roots, key: str, progress) -> list:
    """**Blocking.** Every root's unverified adopted points, checked."""
    import tempfile

    from mmw.core import disk_io
    from mmw.site.adopt import keyring_from
    out = []
    for r in roots:
        progress("phase", text=f"checking {r.info.name}")
        keys = [disk_io.ops.read_bytes(Path(key).expanduser())] if key else r.network_keys()
        if not keys:
            raise ValueError(f"{r.info.name}: no key to check with — give upstream's public key file")
        with tempfile.TemporaryDirectory() as tmp:
            out += r.recheck_signatures(keyring_from(keys, Path(tmp) / "k.gpg"))
    return out


def _rechecked(results) -> str:
    if not results:
        return "nothing to check — every adopted point's signature was checked when it was adopted"
    good = sum(1 for _l, _w, v in results if v == "verified")
    lines = [f"checked {len(results)} point(s): {good} verified"]
    lines += [f"{label}  {what}  {verdict}" for label, what, verdict in results if verdict != "verified"]
    return "\n".join(lines)


def _verify(roots, progress, problems: list[str]) -> list[str]:
    """**Blocking.** What is wrong in each root, added to `problems` as each
    is checked (nothing: all right)."""
    for r in roots:
        progress("phase", text=f"checking every file {r.info.name} serves")
        problems += r.verify()
    return problems


def _verified(problems: list[str]) -> str:
    if not problems:
        return "every served file is there and right"
    shown = problems[:20] + ([f"… and {len(problems) - 20} more"] if len(problems) > 20 else [])
    return "\n".join([f"{len(problems)} problem(s) — Close to repair them:", *shown])


class ActionRunPage(RunPage):
    """One change that can take a while — deleting, cleaning up, combining:
    what it is doing now, each file it places or removes, and what it did.
    No Stop: each is one piece of work that must finish whole."""

    UNIT = "files"
    RATE = "handling"

    def __init__(self, title: str, rows: list[tuple[str, str]], job, said):
        super().__init__(None, rows)
        self.TITLE = title
        self.subtitle = and_list([label for _repo, label in rows])
        self._job, self._said = job, said

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#btn-run-stop", Button).disabled = True

    def updated_note(self) -> str:
        return "done"

    def job(self, progress, stop):
        return self._job(progress)

    def _done(self, result) -> None:
        for repo in list(self._state):
            self._state[repo] = "updated"
        self.query_one("#run-repos", Static).update(self._repo_lines())
        self.query_one("#run-done", Static).update(f"[b $success]✓ {_esc(self._said(result))}[/]")
        self.set_status("")
        self._finish()


class SwitchRunPage(RunPage):
    """Serving versions — one, or every repository as of a bundle: each
    switched in turn, its files placed (counted where a root can say; an
    ssh root says only when it's done). Stop is taken between repositories."""

    UNIT = "files"
    RATE = "placing"
    STOP_NOTE = "stopping finishes the repository switching now; the rest stay as they are"

    def __init__(self, steps, title: str, total: int | None = None):
        steps = [s for s in steps if s.changes]
        super().__init__(total, [(s.key, s.label) for s in steps])
        self.TITLE = title
        self._steps = steps
        self.subtitle = and_list([s.label for s in steps])

    def updated_note(self) -> str:
        return "serving"

    def on_mount(self) -> None:
        super().on_mount()
        if len(self._steps) < 2:                  # one switch: nothing to stop between
            self.query_one("#btn-run-stop", Button).disabled = True

    def job(self, progress, stop):
        return ops.serve_bundle(self._steps, progress=progress, stop_event=stop)

    def _done(self, served) -> None:
        steps = {s.label: s for s in self._steps}
        lines = []
        for label in served:
            s = steps[label]
            what = ("each suite's newest" if s.forward else
                    ", ".join(f"{suite} {_date(date)}" for suite, (date, _i) in sorted(s.target.items())))
            lines.append(f"[b $success]✓ {_esc(label)} serves {_esc(what)}[/]")
        pinned = [label for label in served if not steps[label].forward]
        if pinned:
            lines.append(f"{_esc(and_list(pinned))}: pinned there — Forward (f) moves "
                         f"{'it' if len(pinned) == 1 else 'them'} on")
        self.query_one("#run-done", Static).update("\n".join(lines))
        self.set_status("")
        self._finish()


class TidyRunPage(RunPage):
    """Clearing moved repositories' old copies, root by root — the files
    removed counted where a root can say."""

    TITLE = "Tidying"
    UNIT = "files"
    RATE = "removing"

    def __init__(self, roots: list[str]):
        super().__init__(None, [(name, f"old copies in {name}") for name in roots])
        self.subtitle = "moved repositories' old copies, once nginx is recreated in both roots"

    def on_mount(self) -> None:
        super().on_mount()
        self.query_one("#btn-run-stop", Button).disabled = True

    def updated_note(self) -> str:
        return "tidied"

    def job(self, progress, stop):
        tidied = []
        for root in self.app.mset.roots:
            progress("repo", repo=root.info.name)
            tidied += root.tidy(progress=lambda rel: progress("receive", bytes=1, name=rel))
            progress("finished", repo=root.info.name)
        from mmw.commands.site import removable_conversion_backups
        self._backups = removable_conversion_backups(self.app.mset)
        progress("done")
        return tidied

    def _done(self, tidied) -> None:
        text = (f"[b $success]✓ cleared the old copies of {_esc(and_list(tidied))}[/]" if tidied else
                "nothing to tidy — no moved repository's old copy is left")
        if getattr(self, "_backups", None):
            text += "\n" + _esc("\n".join(self._backups))
        self.query_one("#run-done", Static).update(text)
        self.set_status("")
        self._finish()


class CaSetupPage(Page):
    """Signing needs a CA, and none is set up on this machine: load the
    mirror's existing one, or make one."""

    TITLE = "Certificates"

    def compose_body(self) -> ComposeResult:
        yield Static("HTTPS needs a CA to sign each root's certificate, and this machine has none yet.\n\n"
                     "Load the CA this mirror already uses (its .crt and .key) — clients trust it already — "
                     "or make a new one, whose ca.crt every client then has to trust.\n\n"
                     "The CA is recorded on this machine only, never in a root.", id="ca-setup-text")

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-ca-cancel", compact=True)
        yield Button("Make new…", id="btn-ca-make", compact=True)
        yield Button("Load existing…", id="btn-ca-load", variant="primary", compact=True)

    @on(Button.Pressed, "#btn-ca-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-ca-make")
    def _make(self) -> None:
        self.dismiss("make")

    @on(Button.Pressed, "#btn-ca-load")
    def _load(self) -> None:
        self.dismiss("load")


class MoveRunPage(RunPage):
    """Moving repositories to another root: every file copied, then each
    switched; Stop before the switch leaves everything where it was."""

    TITLE = "Moving"
    RATE = "copying"
    STOP_NOTE = "stopping switches nothing; what was copied stays, and moving again picks up from there"
    UNCHANGED = "nothing was switched"

    def __init__(self, repo: str, from_root: str, to_root: str, preview):
        super().__init__(preview.copies, list(zip(preview.repos, preview.labels)))
        self._repo, self._from, self._to = repo, from_root, to_root
        self._preview = preview
        self.subtitle = f"{and_list(preview.labels)} → {to_root}"

    def received_note(self) -> str:
        return "copied — switches once everything is copied"

    def updated_note(self) -> str:
        return f"moved to {self._to}"

    def job(self, progress, stop):
        return ops.move_repo(self.app.mset, self._repo, self._to, progress=progress, stop_event=stop)

    def _done(self, result) -> None:
        for repo in self._state:
            self._state[repo] = "updated"
        self.query_one("#run-repos", Static).update(self._repo_lines())
        place = next((where(r.info.location) for r in self.app.mset.roots if r.info.name == self._to), "")
        lines = [f"[b $success]✓ moved {_esc(and_list(result.moved))} to {_esc(self._to)} ({_esc(place)})[/]",
                 f"copied {fmt_size(result.copied_bytes)} ({result.files:,} files)"]
        # The hostname routes to the new root once its nginx is recreated;
        # until the old root's is, that one keeps serving its copy.
        lines.append(f"[b $warning]! recreate nginx in {_esc(self._from)} and {_esc(self._to)}, "
                     "then tidy (t) clears the old copy[/]")
        lines += [f"[b $warning]! {_esc(n)}[/]" for n in result.tls_notes]
        self.query_one("#run-done", Static).update("\n".join(lines))
        self.set_status("")
        self._finish()


class MoveRootPage(Page):
    """Which root a repository should live in."""

    TITLE = "Move"

    def __init__(self, label: str, current: str, roots: list[tuple[str, str]]):
        """`roots`: (name, location) — each shown with where it is, so a root
        on another machine never passes for a local one."""
        super().__init__()
        here = dict(roots).get(current)
        self.subtitle = f"{label} · now in {current}" + (f" ({where(here)})" if here else "")
        self._current, self._roots = current, roots

    def compose_body(self) -> ComposeResult:
        from mmw.tui.ui.form import ChoiceField, Form
        yield Form(ChoiceField("Root", [(f"{name} — {where(loc)}", name) for name, loc in self._roots],
                               self._current, id="move-root",
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
