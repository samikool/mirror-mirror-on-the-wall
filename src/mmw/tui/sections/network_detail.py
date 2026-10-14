"""The detail pane for one network: settings, what it selects, what is published.

Everything that touches the pool or the publish tree is loaded off the event
loop, by a worker handed the network it was started for, whose answer is
dropped if another network is showing by the time it lands (`_detail_gen`).

"Published" means reading every mirror's published index — ~23 s for an
88-mirror network on a spinning disk — so: the mirror list renders first
(working it out is instant) with a running count for the sizes; showing the
network already shown starts nothing; a load for a network no longer shown
stops at its next index; and the figures are kept per network, re-read only
when one of the indexes they came from has changed (their mtimes and sizes).
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


def snapshot_line(status) -> str:
    """"3 · serving live · last bundled A → B on <date>" (markup, escaped)."""
    if not status.snapshots and status.serving is None and status.last_bundle is None:
        return "[dim]none yet — n to take one[/]"
    parts = [f"{len(status.snapshots)}"]
    if status.serving is not None:
        parts.append(f"serving {escape(status.name(status.serving))}")
    if status.other_dirs:
        parts.append(f"+{status.other_dirs} other dir{'s' if status.other_dirs > 1 else ''}")
    b = status.last_bundle
    if b is not None:
        day = f"{b.made[:4]}-{b.made[4:6]}-{b.made[6:8]}"
        parts.append(f"last bundled {escape(status.name(b.from_stamp))} → "
                     f"{escape(status.name(b.to_stamp))} on {day}")
    return " · ".join(parts)


class _Superseded(Exception):
    """Another network is showing: stop reading this one's indexes."""


def stat_tree(net, mirror_map, should_stop=None, progress=None) -> tuple:
    """(unique files, unique bytes, newest mtime, per-mirror rows) for a
    network's published tree. **Blocking.** Totals count each file once —
    mirrors share files (arch `all`, `-security` inside `-updates`), so
    summing the rows overcounts. `should_stop()` is checked between mirrors
    (raises `_Superseded`); `progress(done, total)` follows each one."""
    from mmw.core.publisher import publish_base
    from mmw.core.publisher import stats as pub_stats

    published: dict[str, int] = {}
    last_mod: float | None = None
    per_mirror: list[tuple] = []
    total = len(mirror_map)
    for done, (name, mirror) in enumerate(mirror_map.items(), 1):
        if should_stop is not None and should_stop():
            raise _Superseded
        dst_root = publish_base(net, mirror)
        if not dst_root.exists():
            per_mirror.append((name, 0, 0, None, False))
        else:
            try:
                mtime = dst_root.stat().st_mtime
            except OSError:
                mtime = None
            pkgs = pub_stats.dst_packages(dst_root, mirror)
            pub_stats.unique_packages(pkgs, published)
            per_mirror.append((name, len(pkgs), sum(p.size for p in pkgs), mtime, True))
            if mtime is not None and (last_mod is None or mtime > last_mod):
                last_mod = mtime
        if progress is not None:
            progress(done, total)
    return len(published), sum(published.values()), last_mod, per_mirror


def _signature(net, mirror_map) -> tuple:
    """What a cached `stat_tree` answer was read from: each mirror's
    published index (path, mtime, size). Blocking, but only stat()s."""
    from mmw.core.publisher import publish_base
    from mmw.core.publisher.stats import dst_index_path

    sig = []
    for name, mirror in mirror_map.items():
        path = dst_index_path(publish_base(net, mirror), mirror)
        try:
            st = path.stat() if path is not None else None
        except OSError:
            st = None
        sig.append((name, str(path), st.st_mtime_ns if st else 0, st.st_size if st else 0))
    return tuple(sig)


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
        self._snapshots: str | None = None        # the one-line summary, once loaded
        self._detail_gen = 0
        self._progress: tuple[int, int] | None = None      # indexes read / total
        # (network name, root) -> (index signature, stat_tree answer)
        self._cache: dict[tuple, tuple] = {}
        self._rebuild_pending = False
        self._rebuild_lock = asyncio.Lock()

    def compose(self) -> ComposeResult:
        yield Label("No network selected.", classes="empty-msg")

    def show(self, network: config.NetworkConfig | None, cfg: config.Config, *,
             force: bool = False) -> None:
        """Show `network`. The one already showing, under the same config,
        starts nothing — unless `force` (it may have just been republished;
        the cache still spares re-reading an unchanged tree)."""
        if (not force and network is not None and network == self._network
                and cfg is self._cfg):
            return
        self._network = network
        self._cfg = cfg
        self._selected = None
        self._stats = None
        self._error = None
        self._bundle_mtime = None
        self._bundle_loaded = False
        self._snapshots = None
        self._progress = None
        self._detail_gen += 1
        self._rebuild()
        if network is None or cfg is None:
            return
        self._load(self._detail_gen, network, cfg)
        self._load_history(self._detail_gen, cfg.settings.mirror_dir, network)

    # ── loading (off the loop) ───────────────────────────────────────────────

    @work(thread=True, exit_on_error=False, group="network-detail")
    def _load(self, gen: int, net: config.NetworkConfig, cfg: config.Config) -> None:
        try:
            mirror_map = {m.name: m for m in network_mirrors(net, cfg, cfg.settings.mirror_dir)}
            selected = list(mirror_map)
            # The list now; the sizes when they are read.
            self.app.call_from_thread(self._on_selected, gen, selected)
            stats, key, sig = None, None, None
            # A bundle-only network (no `root`) has no published tree to stat —
            # `publish_base()` raises ValueError for exactly this case.
            if net.root is not None:
                key, sig = (net.name, str(net.root)), _signature(net, mirror_map)
                cached = self._cache.get(key)
                if cached is not None and cached[0] == sig:
                    stats = cached[1]
                else:
                    stats = stat_tree(
                        net, mirror_map,
                        should_stop=lambda: gen != self._detail_gen,
                        progress=lambda done, total: self.app.call_from_thread(
                            self._on_progress, gen, done, total))
        except _Superseded:
            return                              # another network is showing
        except Exception as e:  # noqa: BLE001 — shown in the pane, not "Loading…" forever
            self.app.call_from_thread(self._on_load_failed, gen, str(e) or type(e).__name__)
            return
        self.app.call_from_thread(self._on_loaded, gen, selected, stats, key, sig)

    def _on_selected(self, gen: int, selected: list[str]) -> None:
        if gen != self._detail_gen:
            return
        self._selected = selected
        self._rebuild()

    def _on_progress(self, gen: int, done: int, total: int) -> None:
        if gen != self._detail_gen:
            return
        self._progress = (done, total)
        for line in self.query("#stats-progress"):
            line.update(self._progress_text())

    def _progress_text(self) -> str:
        done, total = self._progress or (0, len(self._selected or []))
        return f"[dim]Reading published indexes… {done}/{total}[/]"

    def _on_load_failed(self, gen: int, error: str) -> None:
        if gen != self._detail_gen:
            return
        self._error = error
        self._rebuild()

    def _on_loaded(self, gen: int, selected: list[str], stats: "tuple | None",
                   key: "tuple | None" = None, sig: "tuple | None" = None) -> None:
        if gen != self._detail_gen:
            return                              # a newer selection is on screen
        if key is not None and stats is not None:
            self._cache[key] = (sig, stats)
        self._selected = selected
        self._stats = stats
        self._rebuild()

    @work(thread=True, exit_on_error=False, group="network-detail-bundle")
    def _load_history(self, gen: int, mirror_dir: Path, net: config.NetworkConfig) -> None:
        """When this network was last bundled, and its snapshot line. The
        directory and network are passed in, not read off `self`: the worker
        outlives the selection."""
        import calendar
        import time as _time

        from mmw.core.pool import Pool
        from mmw.core.publisher.manifest import TS_FORMAT, latest_manifest_path
        from mmw.core.snapshots import snapshot_status
        mtime, line = None, None
        try:
            with Pool(mirror_dir, read_only=True) as pool:
                status = snapshot_status(pool, net.name, net.root)
            line = snapshot_line(status)
            # The bundle log first (it knows a backwards bundle is the latest);
            # a network last bundled before snapshots existed has only its
            # ledger file.
            if status.last_bundle is not None:
                mtime = calendar.timegm(_time.strptime(status.last_bundle.made, TS_FORMAT))
            else:
                path = latest_manifest_path(mirror_dir, net.name)
                if path is not None:
                    mtime = path.stat().st_mtime
        except Exception:  # noqa: BLE001 — an unreadable pool: no age, not a crash
            pass
        self.app.call_from_thread(self._on_history, gen, mtime, line)

    def _on_history(self, gen: int, mtime: float | None, line: str | None) -> None:
        if gen != self._detail_gen:
            return
        self._bundle_mtime = mtime
        self._bundle_loaded = True
        self._snapshots = line
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
            # Removal waits only for children whose task started; a child
            # mounted while the app is shutting down never starts, so it
            # outlives the removal and the rebuild would collide on its id.
            # Nothing is shown any more — leave it.
            if self.children:
                return
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
        self.mount(self._meta("Snapshots", "…" if not self._bundle_loaded
                              else self._snapshots or "[dim]unreadable[/]"))
        # What it selects — an empty dimension meaning "all" is exactly what a
        # reader needs told; the mirror list alone hides *why* it is that list.
        if not n.select:
            self.mount(self._meta("Publishes", "[i]every mirrored repository[/]"))
        else:
            self.mount(self._meta("Publishes", ""))
            for sel in n.select:
                from mmw.tui.network_screens import select_dims_line
                cfg = getattr(self.app, "cfg", None)
                pairs = select_dims_line(sel, cfg.sources if cfg else [], short=True)
                dims = "  ".join(
                    f"{label} [i]all[/]" if not vals else
                    f"{label} {escape(', '.join(vals[:3]))}" + ("…" if len(vals) > 3 else "")
                    for label, vals in pairs)
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
        elif self._selected is None:
            self.mount(Label("[dim]Loading…[/]", classes="stats-line"))
        elif self._stats is None:
            self.mount(Label(self._progress_text(), id="stats-progress", classes="stats-line"))
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
        # Built whole, then mounted once: mounting rows into a container that
        # is itself still mounting raises MountError.
        rows: list[Label] = []
        if not self._selected:
            rows.append(Label("(none)", classes="empty-msg"))
        per = {name: (files, size, pub)
               for name, files, size, _, pub in (self._stats[3] if self._stats else [])}
        for name in self._selected:
            files, size, published = per.get(name, (0, 0, None))
            if published and (files or size):
                rows.append(Label(f"● [b]{escape(name)}[/]  [dim]{files:,} files · "
                                  f"{fmt_size(size)}[/]", classes="mirror-entry"))
            elif published is False:
                rows.append(Label(f"● [dim]{escape(name)}  — not published[/]",
                                  classes="mirror-entry"))
            else:
                rows.append(Label(f"● {escape(name)}", classes="mirror-entry"))
        self.mount(Vertical(*rows, id="mirror-list"))
