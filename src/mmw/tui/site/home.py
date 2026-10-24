"""Deployed: every repository the site serves, and the selected one's
history beside it (approved layouts A and B)."""
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Static

from mmw.core.format import fmt_size
from mmw.site import ops
from mmw.tui.ui.list_detail import ListDetail
from mmw.tui.ui.page import Page


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
        Binding("r", "refresh", "Refresh"),
    ]

    def __init__(self):
        super().__init__()
        self._states = []
        self._history = []

    def compose_body(self) -> ComposeResult:
        yield Static("", id="site-banner")
        yield ListDetail(
            DataTable(cursor_type="row", zebra_stripes=True, id="site-repos"),
            _HistoryPane(), id="site-split")

    def on_mount(self) -> None:
        self.query_one("#site-banner").display = False
        t = self.query_one("#site-repos", DataTable)
        # Root joins these once a site has more than one (placements, phase 4).
        t.add_columns("Repository", "Serving", "Newest", "State")
        self.query_one("#site-history", DataTable).add_columns("Version", "Date", "Size", "")
        t.focus()
        self.action_refresh()

    # ── loading ──────────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self._load()

    @work(thread=True, exclusive=True, group="site-load", exit_on_error=False)
    def _load(self) -> None:
        from mmw.site.db import SiteDB
        try:
            states = ops.status(self.app.home)
            with SiteDB(self.app.home) as db:
                newest = {s.repo: db.versions(s.repo)[0].number for s in states if db.versions(s.repo)}
        except Exception as e:  # noqa: BLE001 — shown on the page
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        self.app.call_from_thread(self._loaded, states, newest)

    def _loaded(self, states, newest) -> None:
        if not self.is_attached:
            return
        self._states = states
        t = self.query_one("#site-repos", DataTable)
        keep = self.selected_repo()
        t.clear()
        for s in states:
            serving = f"v{s.serving.number} · {_date(s.serving.stamp)}" if s.serving else "—"
            t.add_row(Text(s.label), serving, f"v{newest.get(s.repo, '?')}",
                      "held" if s.held else "current", key=s.repo)
        if keep is not None and keep in {s.repo for s in states}:
            t.move_cursor(row=t.get_row_index(keep))
        if not states:
            self.set_status("nothing applied yet — a applies a bundle")
            self._show_history(None)
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
                rows = ops.history(self.app.home, repo)
            except Exception as e:  # noqa: BLE001
                self.app.call_from_thread(self.set_status, str(e), "error")
                return
        self.app.call_from_thread(self._history_loaded, repo, rows)

    def _history_loaded(self, repo: str | None, rows) -> None:
        if not self.is_attached or repo != self.selected_repo():
            return
        self._history = rows
        t = self.query_one("#site-history", DataTable)
        t.clear()
        for v, total, frees, serving in rows:
            date = _date(v.stamp) + (f" {v.snap_label}" if v.snap_label else "")
            t.add_row(f"{'▶ ' if serving else '  '}v{v.number}", Text(date), fmt_size(total),
                      "serving" if serving else f"frees {fmt_size(frees)}", key=str(v.number))
            if serving:
                t.move_cursor(row=t.row_count - 1)
        state = next((s for s in self._states if s.repo == repo), None)
        note = ""
        if state and state.held and rows:
            note = f"held at v{state.serving.number} · f forward to v{rows[0][0].number}"
        self.query_one("#site-hold-note", Static).update(note)

    def selected_version(self) -> int | None:
        t = self.query_one("#site-history", DataTable)
        if not self._history or t.row_count == 0:
            return None
        return self._history[min(t.cursor_row, len(self._history) - 1)][0].number


class _HistoryPane(Static):
    """The selected repository's versions, and its hold."""

    def compose(self) -> ComposeResult:
        yield DataTable(cursor_type="row", zebra_stripes=True, id="site-history")
        yield Static("", id="site-hold-note")
