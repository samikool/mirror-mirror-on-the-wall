"""Repositories — what mmw mirrors. List grouped by host, detail beside it.

All state lives in `RepositoryList` (tui/repositories.py); this page renders
it and turns keys into calls on it.
"""
import asyncio
from urllib.parse import urlparse

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.markup import escape
from textual.widgets import Input, Label, ListItem, ListView, Static

from mmw.core import config, pool as pool_mod
from mmw.core.format import fmt_size
from mmw.tui import repositories
from mmw.tui.modals import DeleteProgressModal
from mmw.tui.repositories import RepositoryList, Row, replace_source, without_sources
from mmw.tui.source_screens import EditSourcePage
from mmw.tui.ui.dialog import ConfirmDialog
from mmw.tui.ui.list_detail import ListDetail
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button, safe_id


def _dimension(values: list[str], limit: int = 4) -> str:
    """An empty list means *all* — rendering it blank would hide the most
    important thing about a repository."""
    if not values:
        return "[i]all[/]"
    head = ", ".join(escape(v) for v in values[:limit])
    return head + (f"  [dim]+{len(values) - limit} more[/]" if len(values) > limit else "")


def source_line(model: RepositoryList, src: config.SourceConfig) -> str:
    tick = "[$mmw-selected]✓[/]" if model.is_selected(src) else " "
    arrow = "▼" if src.name in model.expanded else "▶"
    if src.name in model.expanded:
        count = ""
    elif model.unresolved(src):
        # "(0)" for a repository that mirrors everything reads as broken. It
        # is not counted yet, which is a different statement.
        count = "  [dim](all — resolved at sync)[/]"
    else:
        count = f"  [dim]({len(model.mirrors_of(src))})[/]"
    # The path, not the whole URL: the row sits under its host heading, and
    # the path is what tells /debian from /debian-security.
    path = (urlparse(src.url).path or "").rstrip("/")
    where = f"  [$mmw-path]{escape(path)}[/]" if path else ""
    return f" {tick} {arrow} [b]{escape(src.name)}[/]{count}{where}"


def mirror_line(model: RepositoryList, src: config.SourceConfig, m: config.MirrorConfig) -> str:
    tick = "[$mmw-selected]✓[/]" if m.name in model.selection else " "
    if m.suite and m.component:
        # Arch matters: a multi-arch source lists suite/component once per arch.
        label = f"{escape(m.suite)} / {escape(m.component)}  [dim]{escape(m.arch or '')}[/]"
    elif m.name.startswith(src.name + "-"):
        label = escape(m.name[len(src.name) + 1:])
    else:
        label = escape(m.name)
    return f"     {tick} {label}"


def source_detail(model: RepositoryList, src: config.SourceConfig) -> str:
    lines = [
        f"[b $primary]{escape(src.name)}[/]",
        "",
        f"[dim]Provider[/]     {escape(src.provider)}",
        f"[dim]Label[/]        {escape(src.label)}",
        f"[dim]Arches[/]       {_dimension(src.arches)}",
    ]
    if src.provider == "apt":
        lines += [f"[dim]Suites[/]       {_dimension(src.suites)}",
                  f"[dim]Components[/]   {_dimension(src.components)}"]
    lines.append(f"[dim]Mirrors[/]      {len(model.mirrors_of(src))}")
    if src.include_sources:
        lines.append("[dim]deb-src[/]      [$mmw-ok]yes[/]")
    if src.dist_upgrade:
        lines.append("[dim]dist-upgrade[/] [$mmw-ok]yes[/]")
    lines += ["", "[dim]Repository[/]", f"[$mmw-path]{escape(src.url)}[/]"]
    return "\n".join(lines)


def mirror_detail(m: config.MirrorConfig) -> str:
    lines = [f"[b $primary]{escape(m.name)}[/]", "",
             f"[dim]Provider[/]     {escape(m.provider)}",
             f"[dim]Arch[/]         {escape(m.arch or '')}"]
    if m.suite:
        lines.append(f"[dim]Suite[/]        {escape(m.suite)}")
    if m.component:
        lines.append(f"[dim]Component[/]    {escape(m.component)}")
    lines += ["", "[dim]Source[/]", f"[$mmw-path]{escape(m.source_url or '')}[/]"]
    return "\n".join(lines)


class RepositoriesPage(Page):
    TITLE = "Repositories"
    SCROLLS = False
    HELP = ("Edit is the only way to [i]narrow[/i] a repository — adding can only widen it.\n"
            "s syncs the ticked repositories, or all of them when none are ticked.\n"
            "Prune drops packages no index references; wipe deletes every downloaded byte.")
    BINDINGS = [
        Binding("space", "tick", "Tick"),
        Binding("ctrl+a", "tick_all", "Tick all"),
        Binding("slash", "find", "Find"),
        Binding("a", "add", "Add"),
        Binding("e", "edit", "Edit"),
        Binding("d", "remove", "Remove"),
        Binding("p", "prune", "Prune pool", show=False),
        Binding("ctrl+w", "wipe", "Wipe pool", show=False),
        Binding("s", "sync", "Sync"),
    ]

    def __init__(self):
        super().__init__()
        self.model: RepositoryList | None = None
        self._rows: list[Row] = []
        # Rebuilds come from handlers and workers alike (a keypress during the
        # 3 s pool read); interleaved clear/extend pairs collide on row ids.
        self._rebuild_lock = asyncio.Lock()

    def compose_body(self) -> ComposeResult:
        yield Input(placeholder="find by name or URL — esc clears", id="repo-filter",
                    compact=True)
        yield ListDetail(ListView(id="repo-list"), Static("", id="repo-detail"),
                         id="repo-panes")

    def compose_actions(self) -> ComposeResult:
        yield Button("Add", id="btn-repo-add", compact=True)
        yield Button("Edit", id="btn-repo-edit", compact=True)
        yield Button("Remove", id="btn-repo-remove", variant="error", compact=True)
        yield Button("Prune pool", id="btn-prune-pool", compact=True)
        yield Button("Wipe pool", id="btn-wipe-pool", variant="warning", compact=True)

    async def on_mount(self) -> None:
        self.model = RepositoryList(self.app.cfg)
        await self.refresh_rows()
        # The list, not the find box (the first focusable widget): an Input
        # swallows typed digits, so 1–4 would stop switching sections.
        self.query_one("#repo-list", ListView).focus()
        self.set_status("Reading the pool…")
        self.run_worker(self._load_pool(), exclusive=True, group="pool")

    async def _load_pool(self) -> None:
        # A pool query: 3290 ms on the live pool's first call. Until it lands
        # the list shows config expansion, which is the right first answer.
        await asyncio.to_thread(self.model.refresh)
        await self.refresh_rows()
        self._status_selection()

    # ── rendering ────────────────────────────────────────────────────────────

    def _item(self, row: Row) -> ListItem:
        if row.kind == "host":
            return ListItem(Label(f"[dim]{escape(row.host)}[/]"),
                            id=safe_id(row.key), classes="host-row", disabled=True)
        if row.kind == "empty":
            return ListItem(Label("     [dim]not listed yet — sync resolves it[/]"),
                            id=safe_id(row.key), disabled=True)
        text = (source_line(self.model, row.source) if row.kind == "source"
                else mirror_line(self.model, row.source, row.mirror))
        item = ListItem(Label(text), id=safe_id(row.key), classes=f"{row.kind}-row")
        ticked = (self.model.is_selected(row.source) if row.kind == "source"
                  else row.mirror.name in self.model.selection)
        item.set_class(ticked, "-ticked")
        return item

    async def refresh_rows(self) -> None:
        async with self._rebuild_lock:
            await self._rebuild()

    async def _rebuild(self) -> None:
        lst = self.query_one("#repo-list", ListView)
        keep = self.highlighted()
        self._rows = self.model.rows()
        await lst.clear()
        await lst.extend([self._item(r) for r in self._rows])
        index = next((i for i, r in enumerate(self._rows)
                      if keep is not None and r.key == keep.key), None)
        if index is None:
            index = next((i for i, r in enumerate(self._rows) if r.kind == "source"), None)
        lst.index = index
        self._show_detail()

    def highlighted(self) -> Row | None:
        """The repository or mirror under the cursor — never a heading, and
        never a row the filter has hidden."""
        lst = self.query_one("#repo-list", ListView)
        if lst.index is None or not (0 <= lst.index < len(self._rows)):
            return None
        row = self._rows[lst.index]
        return row if row.kind in ("source", "mirror") else None

    def _show_detail(self) -> None:
        row = self.highlighted()
        detail = self.query_one("#repo-detail", Static)
        if row is None:
            detail.update("[dim]Nothing highlighted.[/]")
        elif row.kind == "source":
            detail.update(source_detail(self.model, row.source))
        else:
            detail.update(mirror_detail(row.mirror))

    def on_screen_resume(self) -> None:
        super().on_screen_resume()
        # Settings may have pointed mmw at a different pool since this page
        # last looked: re-read so the counts describe the pool now in use.
        if self.model is not None and self.model.cfg is not self.app.cfg:
            self.model.cfg = self.app.cfg
            self.run_worker(self._load_pool(), exclusive=True, group="pool")

    def _status_selection(self) -> None:
        summary = self.model.selection_summary()
        self.set_status(f"{summary} selected" if summary else "")

    @on(ListView.Highlighted, "#repo-list")
    def _highlighted(self) -> None:
        self._show_detail()

    @on(ListView.Selected, "#repo-list")
    async def _selected(self) -> None:
        row = self.highlighted()
        if row is not None and row.kind == "source":
            self.model.toggle_expanded(row.source)
            await self.refresh_rows()

    # ── ticking ──────────────────────────────────────────────────────────────

    async def action_tick(self) -> None:
        row = self.highlighted()
        if row is None:
            return
        if row.kind == "source":
            self.model.toggle_source(row.source)
        else:
            self.model.toggle_mirror(row.mirror)
        await self.refresh_rows()
        self._status_selection()

    async def action_tick_all(self) -> None:
        self.model.toggle_all()
        await self.refresh_rows()
        self._status_selection()

    # ── find ─────────────────────────────────────────────────────────────────

    def action_find(self) -> None:
        self.query_one("#repo-filter", Input).focus()

    @on(Input.Changed, "#repo-filter")
    async def _filter_changed(self, event: Input.Changed) -> None:
        self.model.filter = event.value.strip()
        self.refresh_bindings()
        await self.refresh_rows()

    @on(Input.Submitted, "#repo-filter")
    def _filter_submitted(self) -> None:
        # Enter keeps the filter and hands the keyboard back to the list.
        self.query_one("#repo-list", ListView).focus()

    def action_back(self) -> None:
        box = self.query_one("#repo-filter", Input)
        if box.value or self.focused is box:
            box.value = ""
            self.query_one("#repo-list", ListView).focus()
            return
        super().action_back()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "back" and self.is_mounted:
            box = self.query_one("#repo-filter", Input)
            if box.value or self.focused is box:
                return True      # Esc clears the filter here, even at the root
        return super().check_action(action, parameters)

    def on_descendant_focus(self, event) -> None:
        super().on_descendant_focus(event)
        self.refresh_bindings()  # Esc's meaning follows the find box

    # ── edit · remove · add ──────────────────────────────────────────────────
    # Keys and buttons start a worker: push_screen_wait needs one.

    def _start(self, flow) -> None:
        self.start_flow(flow)

    def action_add(self) -> None:
        self._start(self._add)

    def action_edit(self) -> None:
        self._start(self._edit)

    def action_remove(self) -> None:
        self._start(self._remove)

    @on(Button.Pressed, "#btn-repo-add")
    def _add_pressed(self) -> None:
        self.action_add()

    @on(Button.Pressed, "#btn-repo-edit")
    def _edit_pressed(self) -> None:
        self.action_edit()

    @on(Button.Pressed, "#btn-repo-remove")
    def _remove_pressed(self) -> None:
        self.action_remove()

    def _highlighted_source(self) -> config.SourceConfig | None:
        """A mirror row belongs to exactly one repository, so editing or
        removing from there acts on that repository."""
        row = self.highlighted()
        return None if row is None else row.source

    async def _after_write(self) -> None:
        self.model.cfg = self.app.cfg
        # Off the loop: the model re-reads the pool.
        await asyncio.to_thread(self.model.refresh)
        await self.refresh_rows()
        self._status_selection()

    async def _edit(self) -> None:
        src = self._highlighted_source()
        if src is None:
            self.notify("Highlight a repository to edit.", severity="warning")
            return
        others = self.app.cfg.sources
        edited = await self.app.push_screen_wait(EditSourcePage(
            src, existing_names={s.name for s in others},
            existing_urls={s.identity for s in others}))
        if edited is None:
            return
        try:
            await self.app.save_config(lambda c: replace_source(c, src.identity, edited))
        except Exception as e:  # noqa: BLE001 — shown on the page
            self.set_status(f"Not saved: {e}", "error")
            return
        await self._after_write()
        self.notify(f"Updated {edited.name}.")

    async def _remove(self) -> None:
        targets = self.model.removal_targets(self._highlighted_source())
        if not targets:
            return
        if len(targets) == 1:
            src = targets[0]
            n = len(self.model.mirrors_of(src))
            title = "Remove repository"
            message = (f"Remove [b]{escape(src.name)}[/b] ({n} mirror{'s' if n != 1 else ''})?\n"
                       f"{escape(src.url)}\nThe config entry goes; downloaded files are kept.")
        else:
            title = f"Remove {len(targets)} repositories"
            names = "\n".join(f"  • {escape(s.name)}" for s in targets)
            message = (f"Remove [b]{len(targets)} repositories[/b]?\n{names}\n"
                       "The config entries go; downloaded files are kept.")
        if not await self.app.push_screen_wait(ConfirmDialog(title, message, "Remove")):
            return
        try:
            await self.app.save_config(lambda c: without_sources(c, targets))
        except Exception as e:  # noqa: BLE001
            self.set_status(f"Not removed: {e}", "error")
            return
        self.model.forget(targets)
        await self._after_write()

    async def _add(self) -> None:
        from mmw.tui.add_repository import AddRepositoryPage
        cfg = self.app.cfg
        new_sources = await self.app.push_screen_wait(AddRepositoryPage(
            existing_units=cfg.configured_units(),
            existing_urls={s.identity for s in cfg.sources}))
        if not new_sources:
            return
        counts = {"added": 0, "updated": 0}

        def merge(c: config.Config) -> config.Config:
            merged, counts["added"], counts["updated"] = repositories.merge_new_sources(
                c, new_sources)
            return merged
        try:
            await self.app.save_config(merge)
        except Exception as e:  # noqa: BLE001
            self.set_status(f"Not added: {e}", "error")
            return
        if not (counts["added"] or counts["updated"]):
            self.notify("Nothing new — everything selected is already configured.")
            return
        await self._after_write()
        self.notify(f"Repositories: {repositories.added_summary(counts['added'], counts['updated'])}.")

    # ── the pool ─────────────────────────────────────────────────────────────
    # Helpers are called as `repositories.<name>` so tests can stand in a busy pool.

    def action_prune(self) -> None:
        self._start(self._prune)

    def action_wipe(self) -> None:
        self._start(self._wipe)

    @on(Button.Pressed, "#btn-prune-pool")
    def _prune_pressed(self) -> None:
        self.action_prune()

    @on(Button.Pressed, "#btn-wipe-pool")
    def _wipe_pressed(self) -> None:
        self.action_wipe()

    async def _prune(self) -> None:
        if not await self.app.push_screen_wait(ConfirmDialog(
                "Prune the package pool",
                "Remove pool packages no configured repository's index references?\n"
                "Refs of repositories removed from the config are cleaned up too.\n"
                "Published network trees keep their own hardlinks until republished.",
                "Prune")):
            return
        self.set_status("Pruning…")
        try:
            removed, freed = await asyncio.to_thread(repositories.prune_pool, self.app.cfg)
        except (pool_mod.PoolBusy, ValueError) as e:
            # A running sync's downloads are unreferenced until it reconciles,
            # and with nothing configured every blob looks unwanted: either
            # way pruning now would delete what must be kept.
            self.set_status(str(e), "error")
            return
        if removed == 0:
            self.set_status("Pool is clean — no orphaned packages.", "ok")
            return
        # The pool is the truth behind every count on screen.
        await self._after_write()
        self.set_status(f"Removed {removed:,} orphaned package(s), freed {fmt_size(freed)}.",
                        "ok")

    async def _wipe(self) -> None:
        mirror_dir = self.app.cfg.settings.mirror_dir
        pool_dir = mirror_dir / ".pool"
        # Worth the query: this is the most destructive button in the app,
        # and "delete the pool" is an abstraction until it has a size.
        got = await asyncio.to_thread(repositories.pool_scale, mirror_dir)
        scale = (f"[b]{got[0]:,}[/b] packages · [b]{fmt_size(got[1])}[/b]\n"
                 if got and got[0] else "")
        if not await self.app.push_screen_wait(ConfirmDialog(
                "⚠  Wipe the package pool",
                f"Delete [b]{escape(str(pool_dir))}[/b]?\n{scale}"
                "This is every package and metadata file mmw has downloaded — the pool IS "
                "the storage. The next sync re-downloads all of it.\n"
                "Kept: mmw.toml, keys/, certs/, and any published network tree.",
                "Wipe")):
            return
        # Refused up front if a sync holds the pool — before the progress
        # modal, not after.
        if await asyncio.to_thread(repositories.pool_busy, mirror_dir):
            self.set_status("A sync is using the pool — wiping it now would delete files it "
                            "is still writing. Stop the sync first.", "error")
            return
        await self.app.push_screen_wait(DeleteProgressModal("Wiping pool", [pool_dir]))
        # Whatever the walk could not remove (a stale lock file, say).
        try:
            await asyncio.to_thread(pool_mod.wipe, mirror_dir)
        except pool_mod.PoolBusy as e:
            self.set_status(str(e), "error")
            return
        await self._after_write()
        self.set_status("Pool wiped. The next sync rebuilds it.", "ok")

    # ── sync ─────────────────────────────────────────────────────────────────

    def action_sync(self) -> None:
        """Sync the ticked repositories — or everything when nothing is ticked."""
        selection = self.model.sync_request()
        self._start(lambda: self.app.start_sync(selection))
