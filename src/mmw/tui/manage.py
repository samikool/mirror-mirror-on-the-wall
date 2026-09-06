"""Manage TUI: mirror list + detail panel and the top-level ManageApp."""
import asyncio
import re
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Footer,
    Header,
    Label,
    ListItem,
    ListView,
    Static,
    TabbedContent,
    TabPane,
    Tabs,
)

from mmw.core import config, pool as pool_mod
from mmw.core.format import fmt_size as _fmt_bytes
from mmw.core.resolve import available_mirrors, pooled_mirrors
from mmw.tui.init_wizard import AddMirrorScreen
from mmw.tui.modals import ConfirmModal
from mmw.tui.publish_panel import PublishPanel
from mmw.tui.widgets import Button


# ── Detail panel ──────────────────────────────────────────────────────────────

class MirrorDetail(Static):
    DEFAULT_CSS = """
    MirrorDetail {
        padding: 1 2;
        height: 1fr;
        overflow-y: auto;
    }
    MirrorDetail .field-label {
        color: $text-muted;
        width: 16;
    }
    MirrorDetail .field-value {
        color: $text;
    }
    MirrorDetail .section-title {
        text-style: bold;
        color: $primary;
        margin-bottom: 1;
    }
    MirrorDetail .action-bar {
        margin-top: 2;
        height: auto;
    }
    """

    def __init__(self):
        super().__init__("")
        self._mirror: config.MirrorConfig | None = None

    def show(self, mirror: config.MirrorConfig | None):
        self._mirror = mirror
        self.refresh_display()

    @staticmethod
    def _dimension(values: list[str], limit: int = 4) -> str:
        """An empty list means *all* — rendering it as a blank field left the
        most important thing about a repository invisible."""
        if not values:
            return "[italic]all[/]"
        head = ", ".join(values[:limit])
        return head + (f"  [dim]+{len(values) - limit} more[/]" if len(values) > limit else "")

    def show_source(self, src: config.SourceConfig, mirrors: list | None = None) -> None:
        """`mirrors` is what the pool holds; falls back to config expansion."""
        mirrors = src.to_mirrors() if mirrors is None else mirrors
        provider_color = {"apt": "magenta", "apt-flat": "cyan"}.get(src.provider, "white")
        extra = ""
        if src.provider == "apt":
            extra = (
                f"\n  [dim]Suites[/]        {self._dimension(src.suites)}"
                f"\n  [dim]Components[/]    {self._dimension(src.components)}"
            )
        if src.include_sources:
            extra += "\n  [dim]deb-src[/]       [green]yes[/]"
        if src.dist_upgrade:
            extra += "\n  [dim]dist-upgrade[/]  [green]yes[/]"
        self.update(
            f"[bold primary]{src.name}[/]\n"
            f"{'─' * 40}\n\n"
            f"  [dim]Provider[/]      [{provider_color}]{src.provider}[/]\n"
            f"  [dim]Label[/]         {src.label}\n"
            f"  [dim]Arches[/]        {self._dimension(src.arches)}\n"
            f"  [dim]Mirrors[/]       {len(mirrors)}"
            f"{extra}\n\n"
            f"  [dim]Repository[/]\n"
            f"  [link]{src.url}[/]\n"
        )

    def refresh_display(self):
        if self._mirror is None:
            self.update("No mirror selected.")
            return

        m = self._mirror
        provider_color = {"apt": "magenta", "apt-flat": "cyan"}.get(m.provider, "white")

        suite_line = f"\n  [dim]Suite[/]         {m.suite}" if m.suite else ""
        component_line = f"\n  [dim]Component[/]     {m.component}" if m.component else ""
        src_line = (
            f"\n  [dim]deb-src[/]       {'[green]yes[/]' if m.include_sources else '[dim]no[/]'}"
            if m.provider == "apt" else ""
        )

        self.update(
            f"[bold primary]{m.name}[/]\n"
            f"{'─' * 40}\n\n"
            f"  [dim]Provider[/]      [{provider_color}]{m.provider}[/]\n"
            f"  [dim]Label[/]         {m.label}\n"
            f"  [dim]Arch[/]          {m.arch}"
            f"{suite_line}"
            f"{component_line}"
            f"{src_line}\n\n"
            f"  [dim]Source[/]\n"
            f"  [link]{m.source_url}[/]\n\n"
            f"  [dim]Mirror path[/]\n"
            f"  {m.mirror_base}\n"
        )


def _safe_id(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


# ── Add-network wizard ────────────────────────────────────────────────────────









# ── Edit-network screen ───────────────────────────────────────────────────────



# ── Publish detail panel ──────────────────────────────────────────────────────



# ── Publish panel ─────────────────────────────────────────────────────────────



# ── Main app ──────────────────────────────────────────────────────────────────

class ManageApp(App):
    TITLE = "mirror-mirror-on-the-wall"
    SUB_TITLE = "manage"

    DEFAULT_CSS = """
    Screen {
        layout: vertical;
    }

    TabbedContent {
        height: 1fr;
    }

    TabPane {
        padding: 0;
        height: 1fr;
    }

    #manage-layout {
        height: 1fr;
        layout: horizontal;
    }

    #list-panel {
        width: 36;
        border-right: solid $primary-darken-2;
        background: $surface;
    }

    #list-panel ListView {
        height: 1fr;
    }

    #list-panel .panel-title {
        background: $primary-darken-3;
        color: $text;
        padding: 0 1;
        text-style: bold;
        height: 1;
        width: 100%;
    }

    #detail-panel {
        width: 1fr;
        background: $background;
    }

    #action-bar {
        height: auto;   /* +1 row for border-top; 3-row buttons need 3 content rows */
        align: left middle;
        padding: 0 2;
        border-top: solid $primary-darken-2;
        background: $surface;
        dock: bottom;
        overflow-x: auto;
    }

    /* Default Button min-width is 16, which overflows the detail panel once
       four of them share a row — size to the label instead. */
    #action-bar Button {
        margin-right: 1;
        min-width: 0;
        width: auto;
    }

    #action-bar .spacer {
        width: 1fr;
        height: 1;
    }

    ListItem {
        padding: 0 1;
    }

    ListItem.--highlight {
        background: $primary-darken-1;
    }

    ListItem.item-selected {
        background: $primary-darken-3;
    }

    ListItem.item-selected.--highlight {
        background: $primary-darken-1;
    }

    /* One row per row. `border-top` adds a line to the box and `margin-top`
       adds another, so a source used to occupy three rows for one line of
       text — seven repositories filled a 35-row list almost entirely with
       whitespace, and the highlight bar covered the gap above the row it was
       actually on. The host heading is the only separator needed. */
    ListItem.host-header {
        background: $surface-darken-2;
        color: $text-muted;
        text-style: bold;
        height: 1;
        margin-top: 1;
    }

    ListItem.source-header {
        background: $surface-darken-1;
        height: 1;
    }

    ListItem.source-header > Label {
        text-style: bold;
    }

    ListItem.source-header.--highlight {
        background: $primary-darken-2;
    }

    ListItem.source-header.item-selected {
        background: $primary-darken-3;
    }
    """

    BINDINGS = [
        Binding("1", "manage_tab", "Manage", show=False),
        Binding("2", "publish_tab", "Publish", show=False),
        Binding("a", "add_mirror", "Add"),
        Binding("d", "delete_mirror", "Delete"),
        # Manage-tab only
        # Shown in the footer: multi-select is invisible otherwise, and there
        # is nothing else on screen that hints the list can be ticked at all.
        Binding("space", "toggle_selection", "Select", show=True),
        Binding("ctrl+space", "toggle_selection", show=False),
        Binding("shift+space", "toggle_selection", show=False),
        Binding("ctrl+a", "select_all", "Select All"),
        Binding("s", "launch_sync", "Sync"),
        Binding("p", "prune_pool", "Prune Pool"),
        Binding("w", "wipe_pool", "Wipe Pool"),
        # Publish-tab only
        Binding("e", "edit_network", "Edit"),
        Binding("p", "publish_network", "Publish"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, cfg: config.Config, cfg_dir: Path, initial_tab: str = "tab-manage"):
        super().__init__()
        self._cfg = cfg
        self._cfg_dir = cfg_dir
        # Two levels, because expanded mirror names are a *guess* until the
        # repository has been listed: `arches = []` means "all", so a source can
        # legitimately expand to zero rows here and still be syncable. The
        # repository identity is the thing that always exists.
        self._selected_sources: set[str] = set()   # repository identities
        self._selection: set[str] = set()          # mirror names, within a source
        self._expanded_sources: set[str] = set()
        self._initial_tab = initial_tab
        self._by_source: dict[str, list[config.MirrorConfig]] = {}
        self._available: list[config.MirrorConfig] = []
        self._refresh_available()

    def _refresh_available(self) -> None:
        """Re-read what the pool actually holds.

        Architectures are discovered, so config expansion is narrower than
        reality — a repository mirroring "all arches" would otherwise show one
        mirror here while the pool holds five.
        """
        # `_by_source` holds **pool truth only**. Folding the config-expansion
        # fallback in here made `_unresolved` believe a repository had been
        # listed, so an `arches = []` source rendered its invented `amd64` rows
        # as if they were real.
        pooled = pooled_mirrors(self._cfg, self._cfg.settings.mirror_dir)
        self._by_source = {}
        for m in pooled:
            self._by_source.setdefault(m.source or "", []).append(m)
        self._available = available_mirrors(
            self._cfg, self._cfg.settings.mirror_dir, pooled=pooled)
        # Drop selections for anything that is no longer configured, so a
        # removed source cannot keep voting on what gets synced.
        configured = {s.identity for s in self._cfg.sources}
        self._selected_sources &= configured
        self._selection &= {m.name for m in self._available}

    def _unresolved(self, src: config.SourceConfig) -> bool:
        """True when this source's rows cannot be known without listing upstream.

        `to_mirrors()` is a pure offline function: with `components = []` it
        expands to **nothing**, and with `arches = []` it invents `amd64`. Both
        are fine for a config that spells its dimensions out and useless for one
        that says "all" — which is now the common case. Rather than render a
        guess, say so, and let sync resolve it.
        """
        if self._by_source.get(src.identity):
            return False                      # the pool knows; that is the truth
        if not src.arches:
            return True
        return src.provider == "apt" and bool(src.suites) and not src.components

    def _mirrors_of(self, src: config.SourceConfig) -> list[config.MirrorConfig]:
        """This repository's mirrors: from the pool, else its config expansion
        (nothing synced yet, so an optimistic answer beats an empty one)."""
        if self._unresolved(src):
            return []
        return self._by_source.get(src.identity) or src.to_mirrors()

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="tabs", initial=self._initial_tab):
            with TabPane("Manage", id="tab-manage"):
                with Horizontal(id="manage-layout"):
                    with Vertical(id="list-panel"):
                        yield Static("  Sources", classes="panel-title", id="panel-title")
                        yield ListView(*self._build_items(), id="manage-list")
                    with Vertical(id="detail-panel"):
                        yield MirrorDetail()
                        with Horizontal(id="action-bar"):
                            yield Button("Remove", id="btn-remove", variant="error")
                            yield Button("Prune Pool", id="btn-prune-pool", variant="default")
                            yield Button("Wipe Pool", id="btn-wipe-pool", variant="warning")
                            yield Static(classes="spacer")
                            yield Button("⚠ Re-init", id="btn-reinit", variant="warning")
            with TabPane("Publish", id="tab-publish"):
                pass  # mounted lazily on first visit
        yield Footer()

    @staticmethod
    def _safe_id(name: str) -> str:
        return _safe_id(f"mirror-{name}")

    @staticmethod
    def _source_safe_id(name: str) -> str:
        return _safe_id(f"source-{name}")

    def _mirror_label(self, m: config.MirrorConfig, src_name: str) -> str:
        if m.suite and m.component:
            # Arch matters here: a multi-arch source lists the same
            # suite/component once per arch.
            return f"{m.suite} / {m.component}  [dim]{m.arch}[/]"
        if m.name.startswith(src_name + "-"):
            return m.name[len(src_name) + 1:]
        return m.name

    def _source_label(self, src: config.SourceConfig) -> str:
        mirrors = self._mirrors_of(src)
        expanded = src.name in self._expanded_sources
        check = "[bold green]✓[/] " if self._source_selected(src) else "  "
        indicator = "▼" if expanded else "▶"
        if expanded:
            count_str = ""
        elif self._unresolved(src):
            # Showing "(0)" for a repository that mirrors everything reads as
            # broken. It is not counted yet, which is a different statement.
            count_str = "  [dim](all — resolved at sync)[/dim]"
        else:
            count_str = f"  [dim]({len(mirrors)})[/dim]"
        return f" {check}{indicator} [bold]{src.name}[/bold]{count_str}  [dim]{src.url}[/dim]"

    def _source_selected(self, src: config.SourceConfig) -> bool:
        return src.identity in self._selected_sources

    @staticmethod
    def _host_of(src: config.SourceConfig) -> str:
        from urllib.parse import urlparse
        return (urlparse(src.url).hostname or "?").lower()

    def _sorted_sources(self) -> list[config.SourceConfig]:
        """Grouped by host, then by name — one host commonly serves several
        repositories (packages.microsoft.com/repos/code and /repos/edge)."""
        return sorted(self._cfg.sources, key=lambda s: (self._host_of(s), s.name.lower()))

    def _build_items(self) -> list[ListItem]:
        items = []
        last_host: str | None = None
        for src in self._sorted_sources():
            host = self._host_of(src)
            if host != last_host:
                items.append(ListItem(
                    Label(f" [dim]{host}[/dim]"),
                    id=_safe_id(f"host-{host}"),
                    classes="host-header",
                    disabled=True,   # a heading, not a target
                ))
                last_host = host
            mirrors = self._mirrors_of(src)
            all_sel = self._source_selected(src)
            src_item = ListItem(
                Label(self._source_label(src)),
                id=self._source_safe_id(src.name),
                classes="source-header",
            )
            if all_sel:
                src_item.add_class("item-selected")
            items.append(src_item)
            if src.name in self._expanded_sources and not mirrors:
                items.append(ListItem(
                    Label("   [dim]not listed yet — sync resolves what this "
                          "repository publishes[/dim]"),
                    id=_safe_id(f"empty-{src.name}"),
                    disabled=True,
                ))
            elif src.name in self._expanded_sources:
                for m in mirrors:
                    sel = m.name in self._selection
                    check = "[bold green]✓[/] " if sel else "  "
                    label = self._mirror_label(m, src.name)
                    item = ListItem(Label(f"   {check}{label}"), id=self._safe_id(m.name))
                    if sel:
                        item.add_class("item-selected")
                    items.append(item)
        return items

    def _update_mirror_display(self, mirror: config.MirrorConfig, src_name: str) -> None:
        sel = mirror.name in self._selection
        check = "[bold green]✓[/] " if sel else "  "
        label = self._mirror_label(mirror, src_name)
        try:
            item = self.query_one(f"#{self._safe_id(mirror.name)}", ListItem)
            item.query_one(Label).update(f"   {check}{label}")
            if sel:
                item.add_class("item-selected")
            else:
                item.remove_class("item-selected")
        except Exception:
            pass

    def _update_source_display(self, src: config.SourceConfig) -> None:
        all_sel = self._source_selected(src)
        try:
            item = self.query_one(f"#{self._source_safe_id(src.name)}", ListItem)
            item.query_one(Label).update(self._source_label(src))
            if all_sel:
                item.add_class("item-selected")
            else:
                item.remove_class("item-selected")
        except Exception:
            pass

    def _update_panel_title(self) -> None:
        n = len(self._selected_sources)
        # Count repositories, not expanded rows: "3 selected" beside three
        # ticked repositories is legible; "47 selected" is not.
        extra = len(self._selection) - sum(
            len(self._mirrors_of(s)) for s in self._cfg.sources if self._source_selected(s))
        parts = []
        if n:
            parts.append(f"{n} source{'s' if n != 1 else ''}")
        if extra > 0:
            parts.append(f"{extra} mirror{'s' if extra != 1 else ''}")
        title = f"  Sources  [{', '.join(parts)} selected]" if parts else "  Sources"
        self.query_one("#panel-title", Static).update(title)

    def _highlighted_item(self) -> tuple[str | None, object]:
        """Return ('source', SourceConfig), ('mirror', MirrorConfig), or (None, None)."""
        lv = self.query_one("#manage-list", ListView)
        if lv.highlighted_child is None:
            return None, None
        item_id = lv.highlighted_child.id or ""
        for src in self._cfg.sources:
            if item_id == self._source_safe_id(src.name):
                return "source", src
        mirror_map = {self._safe_id(m.name): m for m in self._available}
        m = mirror_map.get(item_id)
        if m:
            return "mirror", m
        return None, None

    def _source_for_mirror(self, mirror: config.MirrorConfig) -> config.SourceConfig | None:
        for src in self._cfg.sources:
            if any(m.name == mirror.name for m in self._mirrors_of(src)):
                return src
        return None

    def _selected_mirror(self) -> config.MirrorConfig | None:
        kind, obj = self._highlighted_item()
        return obj if kind == "mirror" else None  # type: ignore[return-value]

    def _save(self):
        config.save(self._cfg, self._cfg_dir)

    async def _refresh_list(self):
        lv = self.query_one("#manage-list", ListView)
        prev_id = lv.highlighted_child.id if lv.highlighted_child else None
        await lv.clear()
        await lv.extend(self._build_items())
        if prev_id:
            for i, item in enumerate(lv.query(ListItem)):
                if item.id == prev_id:
                    lv.index = i
                    break

    @on(ListView.Highlighted, "#manage-list")
    def mirror_highlighted(self, event: ListView.Highlighted):
        kind, obj = self._highlighted_item()
        detail = self.query_one(MirrorDetail)
        if kind == "mirror":
            detail.show(obj)  # type: ignore[arg-type]
        elif kind == "source":
            detail.show_source(obj, self._mirrors_of(obj))  # type: ignore[arg-type]
        else:
            detail.show(None)

    @on(ListView.Selected, "#manage-list")
    def mirror_selected(self, event: ListView.Selected):
        kind, obj = self._highlighted_item()
        if kind == "source":
            self._toggle_expand(obj)  # type: ignore[arg-type]
        elif kind == "mirror":
            self.query_one(MirrorDetail).show(obj)  # type: ignore[arg-type]
        else:
            self.query_one(MirrorDetail).show(None)

    @work(exit_on_error=False)
    async def _toggle_expand(self, src: config.SourceConfig) -> None:
        src_id = self._source_safe_id(src.name)
        if src.name in self._expanded_sources:
            self._expanded_sources.discard(src.name)
        else:
            self._expanded_sources.add(src.name)
        await self._refresh_list()
        lv = self.query_one("#manage-list", ListView)
        for i, item in enumerate(lv.query(ListItem)):
            if item.id == src_id:
                lv.index = i
                break

    def action_toggle_selection(self) -> None:
        """Space toggles the highlighted source or mirror.

        Selecting a *source* records its repository identity, not the names of
        the rows under it: with `arches = []` there may be no rows yet, and an
        empty name set is a subset of everything — which is what silently made
        every source untoggleable.
        """
        kind, obj = self._highlighted_item()
        if kind == "source":
            src: config.SourceConfig = obj  # type: ignore[assignment]
            mirrors = self._mirrors_of(src)
            if self._source_selected(src):
                self._selected_sources.discard(src.identity)
                self._selection -= {m.name for m in mirrors}
            else:
                self._selected_sources.add(src.identity)
                self._selection |= {m.name for m in mirrors}
            self._update_source_display(src)
            for m in mirrors:
                self._update_mirror_display(m, src.name)
        elif kind == "mirror":
            mirror: config.MirrorConfig = obj  # type: ignore[assignment]
            if mirror.name in self._selection:
                self._selection.discard(mirror.name)
            else:
                self._selection.add(mirror.name)
            src = self._source_for_mirror(mirror)
            if src:
                self._sync_source_flag(src)
                self._update_mirror_display(mirror, src.name)
                self._update_source_display(src)
        self._update_panel_title()

    def _sync_source_flag(self, src: config.SourceConfig) -> None:
        """A source counts as selected once every row under it is."""
        mirrors = self._mirrors_of(src)
        if mirrors and all(m.name in self._selection for m in mirrors):
            self._selected_sources.add(src.identity)
        else:
            self._selected_sources.discard(src.identity)

    @work(exit_on_error=False)
    async def action_select_all(self) -> None:
        all_sources = {s.identity for s in self._cfg.sources}
        if self._selected_sources == all_sources:
            self._selected_sources.clear()
            self._selection.clear()
        else:
            self._selected_sources = all_sources
            self._selection = {m.name for m in self._available}
        await self._refresh_list()
        self._update_panel_title()

    @on(Button.Pressed, "#btn-remove")
    @work(exit_on_error=False)
    async def action_delete_mirror(self):
        if self._on_publish_tab():
            panel = self._publish_panel()
            if panel:
                panel.remove_network()
            return

        # Determine which sources to remove: those whose full mirror set is selected,
        # plus the source/mirror currently highlighted if nothing is selected.
        targets: list[config.SourceConfig] = [
            src for src in self._cfg.sources if self._source_selected(src)
        ]
        if not targets:
            kind, obj = self._highlighted_item()
            if kind == "source":
                targets = [obj]  # type: ignore[list-item]
            elif kind == "mirror":
                src = self._source_for_mirror(obj)  # type: ignore[arg-type]
                if src:
                    targets = [src]
        if not targets:
            return

        if len(targets) == 1:
            title = "Remove Source"
            src = targets[0]
            n = len(self._mirrors_of(src))
            msg = (
                f"Remove [bold]{src.name}[/bold]  ({n} repo{'s' if n != 1 else ''})?\n"
                f"  {src.url}\n"
                "Config entry removed — downloaded files are kept."
            )
        else:
            title = f"Remove {len(targets)} Sources"
            names = "\n".join(f"  • {s.name}" for s in targets)
            msg = f"Remove [bold]{len(targets)} sources[/bold]?\n{names}\nConfig entries removed — downloaded files are kept."

        confirmed = await self.push_screen_wait(ConfirmModal(title, msg))
        if confirmed:
            remove_names = {s.name for s in targets}
            self._cfg.sources = [s for s in self._cfg.sources if s.name not in remove_names]
            self._selection.clear()
            self._selected_sources.clear()
            self._save()
            self._refresh_available()
            await self._refresh_list()
            self._update_panel_title()
            self.query_one(MirrorDetail).show(None)

    @on(Button.Pressed, "#btn-wipe-pool")
    @work(exit_on_error=False)
    async def action_wipe_pool(self):
        pool_dir = self._cfg.settings.mirror_dir / ".pool"
        confirmed = await self.push_screen_wait(
            ConfirmModal(
                "⚠  Wipe Package Pool",
                f"Delete [bold]{pool_dir}[/bold]?\n"
                "All pooled packages and the deduplication index will be removed.\n"
                "Existing mirror files are kept. Next sync will re-download everything.\n"
                "Are you sure?",
            )
        )
        if confirmed:
            pool_mod.wipe(self._cfg.settings.mirror_dir)
            self.notify("Pool wiped. Next sync will rebuild it.", severity="warning")

    @on(Button.Pressed, "#btn-prune-pool")
    @work(exit_on_error=False)
    async def action_prune_pool(self):
        confirmed = await self.push_screen_wait(
            ConfirmModal(
                "Prune Package Pool",
                "Remove pool packages no configured mirror's index references?\n"
                "Refs of mirrors removed from the config are cleaned up too.\n"
                "Published network trees keep their own hardlinks until republished.",
            )
        )
        if confirmed:
            mirror_dir = self._cfg.settings.mirror_dir
            from mmw.core.pool import Pool
            configured = [s.identity for s in self._cfg.sources]
            pool = Pool(mirror_dir)
            try:
                removed, freed = await asyncio.to_thread(pool.prune_orphans, configured)
            finally:
                pool.close()
            if removed == 0:
                self.notify("Pool is clean — no orphaned packages found.")
            else:
                self.notify(
                    f"Removed {removed:,} orphaned package(s), freed {_fmt_bytes(freed)}.",
                    severity="warning",
                )

    @on(Button.Pressed, "#btn-reinit")
    @work(exit_on_error=False)
    async def action_reinit(self):
        confirmed = await self.push_screen_wait(
            ConfirmModal(
                "⚠  Destructive: Re-initialize",
                "This will [bold]delete mmw.toml[/bold] and restart setup.\n"
                "Your downloaded packages are NOT deleted.\n"
                "Are you sure?",
            )
        )
        if confirmed:
            config.delete(self._cfg_dir)
            self.exit(result="reinit")

    @work(exit_on_error=False)
    async def action_add_mirror(self):
        if self.query_one(TabbedContent).active == "tab-publish":
            try:
                self.query_one(PublishPanel).add_network()
            except Exception:
                self.notify("Switch to the Publish tab first.", severity="warning")
            return

        new_sources = await self.push_screen_wait(
            AddMirrorScreen(
                existing_units=self._cfg.configured_units(),
                existing_urls={s.identity for s in self._cfg.sources},
            )
        )
        if not new_sources:
            return
        # Merge rather than name-match: picking a new arch for a distro that is
        # already configured extends that source instead of being dropped.
        merged_list, added, merged = config.merge_sources(self._cfg.sources, new_sources)
        if not (added or merged):
            self.notify("Nothing new — everything selected is already configured.")
            return
        self._cfg.sources = merged_list
        self._save()
        # Without this the list — and `action_launch_sync`'s gate — keep the
        # answer computed at startup, so adding to an empty config still
        # reported "no mirrors configured".
        self._refresh_available()
        await self._refresh_list()
        parts = []
        if added:
            parts.append(f"added {added} source(s)")
        if merged:
            parts.append(f"updated {merged}")
        self.notify(f"Sources: {', '.join(parts)}.")

    @work(exit_on_error=False)
    async def action_launch_sync(self):
        if not self._cfg.sources:
            self.notify("No repositories configured — press A to add one.",
                        severity="warning")
            return
        # Hand back a selection, not MirrorConfigs: "all arches" is resolved by
        # listing upstream at launch, so the concrete mirror set is not known
        # here. Repository identities survive that resolution; expanded mirror
        # names may not. An empty selection means everything.
        covered = {m.name
                   for s in self._cfg.sources if self._source_selected(s)
                   for m in self._mirrors_of(s)}
        self.exit(result=("sync", (sorted(self._selected_sources),
                                   sorted(self._selection - covered))))

    def action_edit_network(self) -> None:
        panel = self._publish_panel()
        if panel:
            panel.edit_network()

    def action_publish_network(self) -> None:
        panel = self._publish_panel()
        if panel:
            panel.do_publish()

    def _on_publish_tab(self) -> bool:
        return self.query_one(TabbedContent).active == "tab-publish"

    def _publish_panel(self) -> "PublishPanel | None":
        try:
            return self.query_one(PublishPanel)
        except Exception:
            return None

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        try:
            on_publish = self._on_publish_tab()
        except Exception:
            return True
        manage_only = {"launch_sync", "prune_pool", "wipe_pool", "toggle_selection", "select_all"}
        publish_only = {"publish_network", "edit_network"}
        if on_publish and action in manage_only:
            return False
        if not on_publish and action in publish_only:
            return False
        return True

    def on_key(self, event) -> None:
        if event.key == "down" and isinstance(self.focused, Tabs):
            active = self.query_one(TabbedContent).active
            try:
                if active == "tab-manage":
                    self.query_one(ListView).focus()
                else:
                    self.query_one("#pub-list", ListView).focus()
            except Exception:
                pass
            event.prevent_default()
            event.stop()

        elif event.key == "up" and isinstance(self.focused, ListView):
            if (self.focused.index or 0) == 0:
                try:
                    self.query_one(Tabs).focus()
                except Exception:
                    pass
                event.prevent_default()
                event.stop()

    @on(TabbedContent.TabActivated)
    def _on_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane and event.pane.id == "tab-publish":
            if not event.pane.query(PublishPanel):
                event.pane.mount(PublishPanel(self._cfg, self._cfg_dir))
        self.refresh_bindings()

    def action_manage_tab(self) -> None:
        self.query_one(TabbedContent).active = "tab-manage"

    def action_publish_tab(self) -> None:
        self.query_one(TabbedContent).active = "tab-publish"
