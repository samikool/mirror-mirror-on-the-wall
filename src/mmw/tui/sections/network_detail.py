"""The detail pane for one network: settings, what it selects, what is published.

Everything that touches the pool or the publish tree is loaded off the event
loop, once per `show()`, by a worker handed the network it was started for —
and its answer is dropped if another network is showing by the time it lands
(`_detail_gen`). Before, the mirror list was a pool query run on the loop on
every rebuild, and the stats worker read whichever network was highlighted
when it happened to run.
"""
import asyncio
import time
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.markup import escape
from textual.widget import Widget
from textual.widgets import Label

from mmw.core import config
from mmw.core.format import fmt_size
from mmw.core.resolve import network_mirrors


def _fmt_age(mtime: float) -> str:
    secs = int(time.time() - mtime)
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _on_off(flag: bool) -> str:
    return "[$mmw-ok]on[/]" if flag else "[dim]off[/]"


class NetworkDetail(Widget):
    def __init__(self):
        super().__init__()
        self._network: config.NetworkConfig | None = None
        self._cfg: config.Config | None = None
        self._selected: list[str] | None = None      # mirror names, once loaded
        self._stats: tuple | None = None   # (total_files, total_bytes, last_mod, per_mirror)
        self._error: str | None = None
        self._bundle_mtime: float | None = None
        self._bundle_loaded = False
        self._detail_gen = 0
        self._rebuild_pending = False
        self._rebuild_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        yield Label("No network selected.", classes="empty-msg")

    def show(self, network: config.NetworkConfig | None, cfg: config.Config) -> None:
        self._network = network
        self._cfg = cfg
        self._selected = None
        self._stats = None
        self._error = None
        self._bundle_mtime = None
        self._bundle_loaded = False
        self._detail_gen += 1
        self._rebuild()
        if network is None or cfg is None:
            return
        self._load(self._detail_gen, network, cfg)
        if network.bundle_dir is not None:
            self._load_bundle_age(self._detail_gen, cfg.settings.mirror_dir, network.name)

    # ── loading (off the loop) ───────────────────────────────────────────────

    @work(thread=True, exit_on_error=False, group="network-detail")
    def _load(self, gen: int, net: config.NetworkConfig, cfg: config.Config) -> None:
        try:
            mirror_map = {m.name: m for m in network_mirrors(net, cfg, cfg.settings.mirror_dir)}
            selected = list(mirror_map)
            stats = None
            # A bundle-only network (no `root`) has no published tree to stat —
            # `publish_base()` raises ValueError for exactly this case.
            if net.root is not None:
                stats = self._stat_tree(net, mirror_map)
        except Exception as e:  # noqa: BLE001 — shown in the pane, not "Loading…" forever
            self.app.call_from_thread(self._on_load_failed, gen, str(e) or type(e).__name__)
            return
        self.app.call_from_thread(self._on_loaded, gen, selected, stats)

    def _on_load_failed(self, gen: int, error: str) -> None:
        if gen != self._detail_gen:
            return
        self._error = error
        self._rebuild()

    @staticmethod
    def _stat_tree(net, mirror_map) -> tuple:
        from mmw.core.publisher import dst_stats_apt, dst_stats_apt_flat, publish_base

        total_files = total_bytes = 0
        last_mod: float | None = None
        per_mirror: list[tuple] = []
        for name, mirror in mirror_map.items():
            dst_root = publish_base(net, mirror)
            if not dst_root.exists():
                per_mirror.append((name, 0, 0, None, False))
                continue
            try:
                mtime = dst_root.stat().st_mtime
            except OSError:
                mtime = None
            if mirror.provider == "apt":
                files, size = dst_stats_apt(dst_root, mirror.suite or "",
                                            mirror.component or "main", mirror.arch)
            elif mirror.provider == "apt-flat":
                files, size = dst_stats_apt_flat(dst_root)
            else:
                files, size = 0, 0
            per_mirror.append((name, files, size, mtime, True))
            total_files += files
            total_bytes += size
            if mtime is not None and (last_mod is None or mtime > last_mod):
                last_mod = mtime
        return total_files, total_bytes, last_mod, per_mirror

    def _on_loaded(self, gen: int, selected: list[str], stats: "tuple | None") -> None:
        if gen != self._detail_gen:
            return                              # a newer selection is on screen
        self._selected = selected
        self._stats = stats
        self._rebuild()

    @work(thread=True, exit_on_error=False, group="network-detail-bundle")
    def _load_bundle_age(self, gen: int, mirror_dir: Path, name: str) -> None:
        """When this network was last bundled. The directory and name are
        passed in, not read off `self`: the worker outlives the selection."""
        from mmw.core.publisher.manifest import latest_manifest_path
        mtime = None
        try:
            path = latest_manifest_path(mirror_dir, name)
            if path is not None:
                mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        self.app.call_from_thread(self._on_bundle_age, gen, mtime)

    def _on_bundle_age(self, gen: int, mtime: float | None) -> None:
        if gen != self._detail_gen:
            return
        self._bundle_mtime = mtime
        self._bundle_loaded = True
        self._rebuild()

    # ── rendering ────────────────────────────────────────────────────────────

    def _rebuild(self) -> None:
        """Rebuild after the current message, coalescing bursts.

        `remove_children()` is asynchronous: rebuilding synchronously twice in
        a row mounted the second set beside the first and crashed on the
        duplicate `#mirror-list` id. One pending rebuild at a time, under a
        lock that awaits the removal."""
        if self._rebuild_pending:
            return
        self._rebuild_pending = True
        self.call_later(self._rebuild_now)

    async def _rebuild_now(self) -> None:
        async with self._rebuild_lock:
            self._rebuild_pending = False
            await self.remove_children()
            self._mount_detail()

    def _meta(self, label: str, value: str) -> Label:
        return Label(f"[dim]{label:<13}[/] {value}", classes="detail-meta")

    def _mount_detail(self) -> None:
        n = self._network
        if n is None:
            self.mount(Label("No network selected.", classes="empty-msg"))
            return
        self.mount(Label(f"[b $primary]{escape(n.name)}[/]", classes="detail-title"))
        self.mount(self._meta("Root", escape(str(n.root)) if n.root is not None
                              else "[dim]not set — publish disabled[/]"))
        self.mount(self._meta("Prune", _on_off(n.prune)))
        self.mount(self._meta("Dist-upgrade", _on_off(n.dist_upgrade)))
        self.mount(self._meta("Certs", _on_off(n.certs)))
        # A bundle-only network still needs to show something other than
        # "publish disabled": its destination and the newest ledger entry.
        if n.bundle_dir is not None:
            self.mount(self._meta("Bundle to", escape(str(n.bundle_dir))))
            if not self._bundle_loaded:
                when = "…"
            elif self._bundle_mtime is None:
                when = "never bundled"
            else:
                when = _fmt_age(self._bundle_mtime)
            self.mount(self._meta("Last bundle", when))
        # What it selects — an empty dimension meaning "all" is exactly what a
        # reader needs told; the mirror list alone hides *why* it is that list.
        if not n.select:
            self.mount(self._meta("Publishes", "[i]every mirrored repository[/]"))
        else:
            self.mount(self._meta("Publishes", ""))
            for sel in n.select:
                dims = "  ".join(
                    f"{label} [i]all[/]" if not vals else
                    f"{label} {escape(', '.join(vals[:3]))}" + ("…" if len(vals) > 3 else "")
                    for label, vals in (("suites", sel.suites), ("comps", sel.components),
                                        ("arches", sel.arches)))
                self.mount(Label(f"  [$mmw-path]{escape(sel.source)}[/]", classes="detail-meta"))
                self.mount(Label(f"    [dim]{dims}[/]", classes="detail-meta"))
        self.mount(Label("Published", classes="section-hdr"))
        if self._error:
            self.mount(Label(f"[$mmw-error]✗ couldn't read this network: {escape(self._error)}[/]",
                             classes="stats-line"))
            return
        if n.root is None:
            self.mount(Label("[dim]No publish root set for this network[/]",
                             classes="stats-line"))
        elif self._selected is None or self._stats is None:
            self.mount(Label("[dim]Loading…[/]", classes="stats-line"))
        else:
            total_files, total_bytes, last_mod, per_mirror = self._stats
            if total_files == 0 and all(not pub for *_, pub in per_mirror):
                self.mount(Label("[dim]Not published yet[/]", classes="stats-line"))
            else:
                age = f"  ·  updated {_fmt_age(last_mod)}" if last_mod else ""
                self.mount(Label(f"[b]{fmt_size(total_bytes)}[/]  ·  [b]{total_files:,}[/] files"
                                 f"{age}", classes="stats-line"))
        if self._selected is None:
            return
        self.mount(Label(f"Mirrors ({len(self._selected)})", classes="section-hdr"))
        # A plain Vertical: the pane already scrolls, and a second scroll
        # region inside it only makes the wheel ambiguous.
        mirrors = Vertical(id="mirror-list")
        self.mount(mirrors)
        if not self._selected:
            mirrors.mount(Label("(none)", classes="empty-msg"))
            return
        per = {name: (files, size, pub)
               for name, files, size, _, pub in (self._stats[3] if self._stats else [])}
        for name in self._selected:
            files, size, published = per.get(name, (0, 0, None))
            if published and (files or size):
                mirrors.mount(Label(f"● [b]{escape(name)}[/]  [dim]{files:,} files · "
                                    f"{fmt_size(size)}[/]", classes="mirror-entry"))
            elif published is False:
                mirrors.mount(Label(f"● [dim]{escape(name)}  — not published[/]",
                                    classes="mirror-entry"))
            else:
                mirrors.mount(Label(f"● {escape(name)}", classes="mirror-entry"))
