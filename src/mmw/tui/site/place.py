"""Placing a bundle's hostnames on roots (spec §6, screen 2): read from the
manifest alone, so it opens at once on a bundle of any size. Known
hostnames go where they are; a new one needs a root. Each root's needs are
checked against its free space before Apply is offered."""
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Static

from mmw.core.format import fmt_size
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


class PlacePage(Page):
    TITLE = "Apply"
    SCROLLS = False
    HELP = ("space ticks a hostname (unticked: stored and held, keeps serving what it serves).\n"
            "enter cycles a new hostname's root; Add a root… offers another folder.")
    BINDINGS = [Binding("space", "toggle", "Tick"), Binding("enter", "cycle", "Root"),
                Binding("a", "add", "Add a root")]

    def __init__(self, plan, src: str, mset):
        super().__init__()
        self._plan, self._src, self._mset = plan, src, mset
        self.subtitle = f"{Path(src).name} · network {plan.network} · {plan.created}"
        self.roots: list[str] = list(plan.locations) if mset else []     # names here, then new locations
        self.targets: dict[str, str | None] = {h: (o if o in plan.locations else None)
                                               for h, o in plan.owner.items()}
        self.held_back = {h for h, o in plan.owner.items() if o is not None and o not in plan.locations}
        self.ticked = set(plan.hosts) - self.held_back
        self._cache: dict = {}                     # each root's store hits and free space, checked once

    def compose_body(self) -> ComposeResult:
        yield DataTable(id="place-hosts", cursor_type="row")
        yield Static("", id="place-roots")

    def compose_actions(self) -> ComposeResult:
        yield Button("Add a root…", id="btn-place-add", compact=True)
        yield Button("Cancel", id="btn-place-cancel", compact=True)
        yield Button("Apply", id="btn-place-go", variant="primary", compact=True)

    def on_mount(self) -> None:
        t = self.query_one("#place-hosts", DataTable)
        t.add_columns("", "Hostname", "Repos", "Size", "Root", "")
        self._refresh()
        t.focus()

    # ── state ────────────────────────────────────────────────────────────────

    def _known(self, h: str) -> bool:
        return self._plan.owner.get(h) is not None

    def choose_root(self, host: str, root: str) -> None:
        if self._known(host):
            return                                  # a known hostname stays where it is
        if root not in self.roots:
            self.roots.append(root)
        self.targets[host] = root
        self._refresh()

    def add_root(self, path: str) -> None:
        from mmw.site.home import read_root
        info = read_root(path)
        if info is not None:
            mine = self._mset.set_id if self._mset else None
            if mine is None or info.set_id != mine:
                if mine is not None or self._mset is None:
                    self.set_status(f"{path} holds the mirror of network {info.network!r} — pick another "
                                    "folder", "error")
                    return
            path = info.name
        if path not in self.roots:
            self.roots.append(path)
        for h in self._plan.hosts:                  # new hostnames with no root yet go here
            if not self._known(h) and h not in self.held_back and self.targets.get(h) is None:
                self.targets[h] = path
        self.set_status(f"root {path} added — enter on a new hostname moves it between roots")
        self._refresh()

    def _needs(self) -> dict[str, tuple[int, int]]:
        from mmw.site.apply import placement_needs
        # Every placed hostname lands in its root, ticked or not (unticked:
        # stored and held), so every one counts toward that root's needs.
        placed = {h: t for h, t in self.targets.items() if t is not None}
        return placement_needs(self._mset, self._plan, placed, cache=self._cache)

    def hosts_text(self) -> str:
        t = self.query_one("#place-hosts", DataTable)
        return "\n".join(" ".join(str(c) for c in t.get_row_at(i)) for i in range(t.row_count))

    def roots_text(self) -> str:
        return str(self.query_one("#place-roots", Static).render())

    def status_text(self) -> str:
        return str(self.query_one("#page-status", Static).render())

    def _refresh(self) -> None:
        t = self.query_one("#place-hosts", DataTable)
        keep = t.cursor_row
        t.clear()
        for h, repos in self._plan.hosts.items():
            mark = "☑" if h in self.ticked else "☐"
            where = self.targets.get(h) or ("held back" if h in self.held_back else "choose a root")
            state = "held back" if h in self.held_back else ("known" if self._known(h) else "new")
            t.add_row(mark, h, str(len(repos)), fmt_size(self._plan.sizes[h]), where, state, key=h)
        if t.row_count:
            t.move_cursor(row=min(keep, t.row_count - 1))
        needs = self._needs()
        lines = []
        for key, (need, free) in needs.items():
            ok = need <= free
            where = self._plan.locations.get(key, "new root")
            lines.append(f"{'✓' if ok else '✗'} {key}  ({where})  needs {fmt_size(need)} · {fmt_size(free)} free")
        self.query_one("#place-roots", Static).update(Text("Roots\n" + ("\n".join(lines) or "  none yet")))
        unplaced = [h for h in self._plan.hosts if h not in self.held_back and self.targets.get(h) is None]
        self.query_one("#btn-place-go", Button).disabled = bool(unplaced) or any(
            need > free for need, free in needs.values()) or not self.ticked

    # ── keys ─────────────────────────────────────────────────────────────────

    def _cursor_host(self) -> str | None:
        t = self.query_one("#place-hosts", DataTable)
        if not t.row_count:
            return None
        return list(self._plan.hosts)[t.cursor_row]

    def action_toggle(self) -> None:
        h = self._cursor_host()
        if h is None or h in self.held_back:
            return
        self.ticked ^= {h}
        self._refresh()

    @on(DataTable.RowSelected, "#place-hosts")
    def _row_selected(self, _event) -> None:
        self.action_cycle()               # the table takes enter for itself: this is that enter

    def action_cycle(self) -> None:
        h = self._cursor_host()
        if h is None or self._known(h) or not self.roots:
            return
        now = self.targets.get(h)
        nxt = self.roots[(self.roots.index(now) + 1) % len(self.roots)] if now in self.roots else self.roots[0]
        self.choose_root(h, nxt)

    @on(Button.Pressed, "#btn-place-add")
    def action_add(self) -> None:
        from mmw.tui.path_pick import PathPickPage
        self.app.push_screen(PathPickPage("Add a root", "a folder for hostnames to live in"),
                             lambda path: path and self.add_root(path))

    @on(Button.Pressed, "#btn-place-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-place-go")
    def _go(self) -> None:
        from mmw.tui.site.apply_flow import ApplyChoice
        locations = self._plan.locations
        place = {h: locations.get(t, t) for h, t in self.targets.items()
                 if t is not None and not self._known(h)}
        only = frozenset(r for h in self.ticked for r in self._plan.hosts[h])
        labels = tuple((r, self._plan.labels[r]) for h in self._plan.hosts for r in self._plan.hosts[h])
        total = sum(self._plan.sizes.values()) + self._plan.network_bytes
        self.dismiss(ApplyChoice(self._src, only, total, labels, frozenset(), place=place,
                                 sizes=tuple(self._plan.repo_sizes.items())))
