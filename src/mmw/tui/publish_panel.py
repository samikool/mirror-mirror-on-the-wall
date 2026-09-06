"""Publish tab: network list, detail panel, and the publish/dry-run flows."""
import asyncio
import re
from pathlib import Path

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.widget import Widget
from textual.widgets import Label, ListItem, ListView, Static

from rich.markup import escape as _escape

from mmw.core import config
from mmw.core.resolve import available_mirrors, network_mirrors
from mmw.core.format import fmt_size as _fmt_bytes
from mmw.tui.modals import ConfirmModal, DeleteProgressModal, InfoModal
from mmw.tui.network_screens import AddNetworkScreen, EditNetworkScreen
from mmw.tui.publish_modals import CertsModal, DryRunResultModal, PreparePublishModal, PublishProgressModal
from mmw.tui.widgets import Button


# Verify and dry-run are per-mirror, read-only, and independent. Run a few at a
# time: the expensive parts (lzma/gzip decompress, SQLite) release the GIL, so
# they genuinely overlap, and the modal advances as each lands instead of
# sitting still for the seconds one big index takes. Bounded low on purpose —
# every one of these hits the pool's single shared connection, so more workers
# would just queue on that lock.
_PREPARE_WORKERS = 4


async def _run_per_mirror(mirrors, fn, on_done):
    """Run fn(mirror) off-thread, at most _PREPARE_WORKERS at once.

    Results come back in `mirrors` order; on_done(completed, mirror, result)
    fires as each finishes, in completion order, so progress keeps moving.
    """
    sem = asyncio.Semaphore(_PREPARE_WORKERS)
    results: list = [None] * len(mirrors)
    done = 0

    async def _one(i, m):
        nonlocal done
        async with sem:
            r = await asyncio.to_thread(fn, m)
        results[i] = r
        done += 1
        on_done(done, m, r)

    await asyncio.gather(*(_one(i, m) for i, m in enumerate(mirrors)))
    return results


def exclusive_publish_dirs(
    net: config.NetworkConfig,
    mirror_map: dict[str, config.MirrorConfig],
    removed_names: set[str],
    kept_names: set[str],
) -> tuple[list[Path], list[str]]:
    """Split removed mirrors into (deletable dirs, bases still in use).

    One publish base serves every mirror of a source URL — all Ubuntu suites
    live under archive.ubuntu.com/ubuntu — so deleting the base of a removed
    mirror would take the kept suites with it. Only bases no kept mirror
    publishes to are safe to delete; the rest lose their stale files on the
    next publish's prune pass.
    """
    kept_bases = {mirror_map[n].mirror_base for n in kept_names if n in mirror_map}
    removed_bases = {mirror_map[n].mirror_base for n in removed_names if n in mirror_map}
    dirs = [
        net.root / b
        for b in sorted(removed_bases - kept_bases)
        if (net.root / b).exists()
    ]
    return dirs, sorted(removed_bases & kept_bases)


class NetworkDetail(Widget):
    DEFAULT_CSS = """
    NetworkDetail {
        height: 1fr;
        overflow-y: auto;
        padding: 1 2;
    }
    NetworkDetail .detail-title  { text-style: bold; color: $primary; margin-bottom: 1; }
    NetworkDetail .detail-meta   { color: $text-muted; }
    NetworkDetail .section-hdr   { text-style: bold; color: $primary; margin-top: 1; margin-bottom: 0; }
    NetworkDetail .stats-line    { color: $text; margin-top: 1; }
    NetworkDetail .stats-loading { color: $text-muted; margin-top: 1; }
    NetworkDetail .mirror-entry  { color: $text; margin-left: 1; }
    NetworkDetail .mirror-unpub  { color: $text-muted; margin-left: 1; }
    NetworkDetail .empty-msg     { color: $text-muted; }
    NetworkDetail ScrollableContainer { height: 1fr; margin-top: 1; }
    """

    def __init__(self):
        super().__init__()
        self._network: config.NetworkConfig | None = None
        self._cfg: config.Config | None = None
        self._stats: tuple | None = None   # (total_files, total_bytes, last_mod, per_mirror)

    def compose(self) -> ComposeResult:
        yield Label("No network selected.", classes="empty-msg")

    def show(self, network: config.NetworkConfig | None, cfg: config.Config) -> None:
        self._network = network
        self._cfg = cfg
        self._stats = None
        self._rebuild()
        if network is not None:
            self._load_stats()

    @staticmethod
    def _fmt_age(mtime: float) -> str:
        import time as _time
        secs = int(_time.time() - mtime)
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{secs // 60}m ago"
        if secs < 86400:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"

    def _rebuild(self) -> None:
        self.remove_children()
        if self._network is None:
            self.mount(Label("No network selected.", classes="empty-msg"))
            return
        n = self._network
        prune_str = "[green]on[/]" if n.prune else "[dim]off[/]"
        du_str = "[green]on[/]" if n.dist_upgrade else "[dim]off[/]"
        certs_str = "[green]on[/]" if n.certs else "[dim]off[/]"

        self.mount(Label(f"[bold]{n.name}[/]", classes="detail-title"))
        self.mount(Label(f"  Root          {n.root}", classes="detail-meta"))
        self.mount(Label(f"  Prune         {prune_str}", classes="detail-meta"))
        self.mount(Label(f"  Dist-upgrade  {du_str}", classes="detail-meta"))
        self.mount(Label(f"  Certs         {certs_str}", classes="detail-meta"))

        # What this network selects. Without this the only visible thing is the
        # resulting mirror list, which hides *why* it is that list — and an
        # empty dimension meaning "all" is exactly what a reader needs told.
        if not n.select:
            self.mount(Label("  Publishes     [italic]every mirrored repository[/]",
                             classes="detail-meta"))
        else:
            self.mount(Label("  Publishes", classes="detail-meta"))
            for sel in n.select:
                dims = "  ".join(
                    f"{label} [italic]all[/]" if not vals else f"{label} {', '.join(vals[:3])}"
                    + ("…" if len(vals) > 3 else "")
                    for label, vals in (
                        ("suites", sel.suites),
                        ("comps", sel.components),
                        ("arches", sel.arches),
                    )
                )
                self.mount(Label(f"    [dim]{_escape(sel.source)}[/]", classes="detail-meta"))
                self.mount(Label(f"      [dim]{dims}[/]", classes="detail-meta"))

        # Stats section
        self.mount(Label("  Published", classes="section-hdr"))
        if self._stats is None:
            self.mount(Label("  [dim]Loading…[/]", classes="stats-loading"))
        else:
            total_files, total_bytes, last_mod, per_mirror = self._stats
            if total_files == 0 and all(not pub for *_, pub in per_mirror):
                self.mount(Label("  [dim]Not published yet[/]", classes="stats-loading"))
            else:
                age_str = f"  ·  updated {self._fmt_age(last_mod)}" if last_mod else ""
                self.mount(Label(
                    f"  [bold]{_fmt_bytes(total_bytes)}[/]  ·  [bold]{total_files:,}[/] files{age_str}",
                    classes="stats-line",
                ))

        # Mirror breakdown
        selected = (
            [m.name for m in network_mirrors(n, self._cfg, self._cfg.settings.mirror_dir)]
            if self._cfg else []
        )
        self.mount(Label(f"  Mirrors ({len(selected)})", classes="section-hdr"))
        scroll = ScrollableContainer()
        self.mount(scroll)
        if not selected:
            scroll.mount(Label("  (none)", classes="empty-msg"))
            return

        if self._stats is not None:
            _, _, _, per_mirror = self._stats
            pm_map = {name: (files, size, published) for name, files, size, _, published in per_mirror}
            for name in selected:
                if name in pm_map:
                    files, size, published = pm_map[name]
                    if published and (files or size):
                        scroll.mount(Label(
                            f"  ● [bold]{name}[/]  [dim]{files:,} files · {_fmt_bytes(size)}[/]",
                            classes="mirror-entry",
                        ))
                    else:
                        scroll.mount(Label(
                            f"  ● [dim]{name}  — not published[/]",
                            classes="mirror-unpub",
                        ))
                else:
                    scroll.mount(Label(f"  ● [dim]{name}[/]", classes="mirror-entry"))
        else:
            for name in selected:
                scroll.mount(Label(f"  ● {name}", classes="mirror-entry"))

    @work(thread=True, exit_on_error=False)
    def _load_stats(self) -> None:
        from mmw.core.publisher import (
            publish_base, dst_stats_apt, dst_stats_apt_flat,
        )
        net = self._network
        cfg = self._cfg
        if net is None or cfg is None:
            return
        mirror_map = {m.name: m for m in network_mirrors(net, cfg, cfg.settings.mirror_dir)}
        selected = list(mirror_map)
        total_files = total_bytes = 0
        last_mod: float | None = None
        per_mirror: list[tuple] = []

        for name in selected:
            mirror = mirror_map.get(name)
            if mirror is None:
                per_mirror.append((name, 0, 0, None, False))
                continue
            dst_root = publish_base(net, mirror)
            if not dst_root.exists():
                per_mirror.append((name, 0, 0, None, False))
                continue
            try:
                mtime = dst_root.stat().st_mtime
            except OSError:
                mtime = None
            if mirror.provider == "apt":
                files, size = dst_stats_apt(dst_root, mirror.suite or "", mirror.component or "main", mirror.arch)
            elif mirror.provider == "apt-flat":
                files, size = dst_stats_apt_flat(dst_root)
            else:
                files, size = 0, 0
            per_mirror.append((name, files, size, mtime, True))
            total_files += files
            total_bytes += size
            if mtime is not None and (last_mod is None or mtime > last_mod):
                last_mod = mtime

        self.call_from_thread(self._on_stats_loaded, total_files, total_bytes, last_mod, per_mirror)

    def _on_stats_loaded(self, total_files: int, total_bytes: int, last_mod: float | None, per_mirror: list) -> None:
        if self._network is None:
            return
        self._stats = (total_files, total_bytes, last_mod, per_mirror)
        self._rebuild()

class PublishPanel(Widget):
    DEFAULT_CSS = """
    PublishPanel {
        height: 1fr;
        layout: horizontal;
    }
    PublishPanel #pub-list-panel {
        width: 30;
        border-right: solid $primary-darken-2;
        background: $surface;
    }
    PublishPanel #pub-list-panel .panel-title {
        background: $primary-darken-3;
        color: $text;
        padding: 0 1;
        text-style: bold;
        height: 1;
        width: 100%;
    }
    PublishPanel #pub-list-panel ListView {
        height: 1fr;
    }
    PublishPanel #pub-detail-panel {
        width: 1fr;
        background: $background;
    }
    PublishPanel #pub-action-bar {
        height: auto;   /* +1 row for border-top; 3-row buttons need 3 content rows */
        align: left middle;
        padding: 0 2;
        border-top: solid $primary-darken-2;
        background: $surface;
        dock: bottom;
    }
    /* Default Button min-width is 16; five of them overflow the detail panel
       on an 80- or 100-column terminal. Size to the label and scroll if the
       row still cannot fit. */
    PublishPanel #pub-action-bar Button {
        margin-right: 1;
        min-width: 0;
        width: auto;
    }
    """

    BINDINGS = [
        Binding("c", "certs", "Certs"),
    ]

    def __init__(self, cfg: config.Config, cfg_dir: Path):
        super().__init__()
        self._cfg = cfg
        self._cfg_dir = cfg_dir

    def compose(self) -> ComposeResult:
        with Vertical(id="pub-list-panel"):
            yield Static("  Networks", classes="panel-title")
            yield ListView(*self._build_items(), id="pub-list")
        with Vertical(id="pub-detail-panel"):
            yield NetworkDetail()
            with Horizontal(id="pub-action-bar"):
                yield Button("Add", id="btn-add-network", variant="primary")
                yield Button("Edit", id="btn-edit-network", variant="default")
                yield Button("Remove", id="btn-remove-network", variant="error")
                yield Button("Publish", id="btn-publish", variant="success")
                yield Button("Dry Run", id="btn-dry-run", variant="default")

    def _available(self) -> "list[config.MirrorConfig]":
        """Mirrors that actually exist — from the pool, not config expansion.

        Architectures are discovered, so `cfg.mirrors` under-reports a
        repository mirroring "all" of them.
        """
        if not self._cfg:
            return []
        return available_mirrors(self._cfg, self._cfg.settings.mirror_dir)

    @staticmethod
    def _net_id(name: str) -> str:
        return "net-" + re.sub(r"[^a-zA-Z0-9_-]", "-", name)

    def _build_items(self) -> list[ListItem]:
        return [ListItem(Label(f"  {n.name}"), id=self._net_id(n.name)) for n in self._cfg.networks]

    def _selected_network(self) -> config.NetworkConfig | None:
        lv = self.query_one("#pub-list", ListView)
        if lv.highlighted_child is None:
            return None
        item_id = lv.highlighted_child.id or ""
        return next((n for n in self._cfg.networks if self._net_id(n.name) == item_id), None)

    def _save(self):
        config.save(self._cfg, self._cfg_dir)

    async def _refresh_list(self, select_name: str | None = None):
        lv = self.query_one("#pub-list", ListView)
        await lv.clear()
        await lv.extend(self._build_items())
        if select_name:
            target_id = self._net_id(select_name)
            for i, item in enumerate(lv.query(ListItem)):
                if item.id == target_id:
                    lv.index = i
                    break

    @on(ListView.Highlighted, "#pub-list")
    def network_highlighted(self, event: ListView.Highlighted):
        net = self._selected_network()
        self.query_one(NetworkDetail).show(net, self._cfg)

    @on(Button.Pressed, "#btn-edit-network")
    def _edit_network_btn(self) -> None:
        self.edit_network()

    @on(Button.Pressed, "#btn-add-network")
    @work(exit_on_error=False)
    async def add_network(self):
        existing = {n.name for n in self._cfg.networks}
        net = await self.app.push_screen_wait(AddNetworkScreen(self._available(), existing))
        if net is None:
            return
        if any(n.name == net.name for n in self._cfg.networks):
            self.app.notify(f"Network '{net.name}' already exists.", severity="warning")
            return
        self._cfg.networks.append(net)
        self._save()
        await self._refresh_list(select_name=net.name)
        self.query_one(NetworkDetail).show(net, self._cfg)

    @work(exit_on_error=False)
    async def edit_network(self):
        net = self._selected_network()
        if net is None:
            self.app.notify("Select a network first.", severity="warning")
            return

        available = self._available()
        mirror_map = {m.name: m for m in available}
        mirror_dir = self._cfg.settings.mirror_dir
        old_mirrors = {m.name for m in network_mirrors(net, self._cfg, mirror_dir)}

        result = await self.app.push_screen_wait(
            EditNetworkScreen(net, available)
        )
        if result is None:
            return

        new_select, prune, dist_upgrade, certs = result
        net.select, net.prune, net.dist_upgrade, net.certs = new_select, prune, dist_upgrade, certs
        new_set = {m.name for m in network_mirrors(net, self._cfg, mirror_dir)}
        removed_names = old_mirrors - new_set
        self._save()
        self.query_one(NetworkDetail).show(net, self._cfg)

        if removed_names:
            dirs, shared_bases = exclusive_publish_dirs(net, mirror_map, removed_names, new_set)
            if shared_bases:
                where = "the next publish prunes them" if net.prune else (
                    "prune is off for this network, so they stay until you turn it on"
                )
                self.app.notify(
                    f"{len(shared_bases)} removed mirror(s) share a published tree with "
                    f"mirrors you kept — those files stay until {where}.",
                    severity="information",
                    timeout=12,
                )
            if dirs:
                paths = "\n".join(f"  [dim]{d}[/dim]" for d in dirs)
                msg = f"Delete published files for removed mirror(s)?\n{paths}"
                if await self.app.push_screen_wait(ConfirmModal("Delete Published Files?", msg, confirm_label="Yes", cancel_label="No", confirm_variant="success", cancel_variant="default")):
                    await self.app.push_screen_wait(
                        DeleteProgressModal(f"Deleting published files — {net.name}", dirs)
                    )

    @on(Button.Pressed, "#btn-remove-network")
    @work(exit_on_error=False)
    async def remove_network(self):
        net = self._selected_network()
        if net is None:
            return
        if not await self.app.push_screen_wait(
            ConfirmModal("Remove Network", f"Remove network [bold]{net.name}[/bold]?")
        ):
            return

        self._cfg.networks = [n for n in self._cfg.networks if n.name != net.name]
        self._save()
        await self._refresh_list()
        self.query_one(NetworkDetail).show(None, self._cfg)

        if net.root.exists():
            msg = f"Also delete all published files?\n  [dim]{net.root}[/dim]"
            if await self.app.push_screen_wait(
                ConfirmModal("Delete Published Files?", msg, confirm_label="Yes", cancel_label="No",
                             confirm_variant="success", cancel_variant="default")
            ):
                await self.app.push_screen_wait(
                    DeleteProgressModal(f"Deleting — {net.name}", [net.root])
                )

    async def _run_pre_publish(
        self,
        net: "config.NetworkConfig",
        selected_mirrors: list,
        mirror_dir: "Path",
        include_dry_run: bool = False,
        include_file_counts: bool = True,
        include_verify: bool = True,
    ):
        """Run verify (+ optional dry run) with a PreparePublishModal.

        Returns (verify_results, file_counts, dry_run_result).
        file_counts: dict[mirror_name, int] — actual package file count for the first
        mirror sharing each mirror_base; 0 for subsequent mirrors in the same base
        (they skip instantly since the first one did the work).
        dry_run_result is None when include_dry_run=False.
        include_verify=False skips phase 1 — used when the caller just verified
        (dry-run → confirm → publish) to avoid re-parsing every index.
        """
        from mmw.core.verify import verify_mirror
        from mmw.core.pool import Pool
        from mmw.core.publisher import (
            dry_run_mirror, NetworkPublishResult, dist_upgrade_info,
        )

        modal = PreparePublishModal(net.name)
        await self.app.push_screen(modal)
        verify_results = []
        file_counts: dict[str, int] = {}
        dry_result = None

        try:
            with Pool(mirror_dir, read_only=True) as pool:
                # ── Phase 1: verify ───────────────────────────────────────
                if include_verify:
                    modal.set_phase("Verifying mirrors", len(selected_mirrors))

                    def _report_verify(n, m, vr):
                        if vr.error:
                            detail, ok = vr.error, False
                        elif vr.complete:
                            detail, ok = f"complete · {vr.total:,} files", True
                        else:
                            detail, ok = f"{vr.missing_count:,} of {vr.total:,} missing", False
                        modal.mirror_done(n, m.name, detail, ok)

                    verify_results = await _run_per_mirror(
                        selected_mirrors,
                        lambda m: verify_mirror(m, mirror_dir, pool),
                        _report_verify,
                    )

                # ── Phase 2: unique package count per base group (DB query) ──
                # Skipped during dry-run — only needed when proceeding to publish.
                if include_file_counts:
                    modal.set_phase("Counting packages...", len(selected_mirrors))
                    from collections import defaultdict
                    base_to_names: dict[str, list[str]] = defaultdict(list)
                    for m in selected_mirrors:
                        base_to_names[m.mirror_base].append(m.name)

                    seen_assigned: set[str] = set()
                    for m in selected_mirrors:
                        if m.mirror_base not in seen_assigned:
                            seen_assigned.add(m.mirror_base)
                            names = base_to_names[m.mirror_base]
                            count = await asyncio.to_thread(pool.group_unique_count, names)
                            file_counts[m.name] = count
                        else:
                            file_counts[m.name] = 0

                # ── Phase 3: dry run (optional) ───────────────────────────
                if include_dry_run:
                    dry_result = NetworkPublishResult(network_name=net.name, dry_run=True)
                    modal.set_phase("Calculating changes", len(selected_mirrors))

                    def _report_dry(n, m, mr):
                        if mr.errors:
                            detail, ok = mr.errors[0], False
                        else:
                            detail = f"{mr.total_files:,} files · {_fmt_bytes(mr.total_bytes)}"
                            if mr.linked > 0:
                                detail += f"  →  {mr.linked:,} to add ({_fmt_bytes(mr.linked_bytes)})"
                            ok = True
                        modal.mirror_done(n, m.name, detail, ok)

                    dry_result.mirrors = await _run_per_mirror(
                        selected_mirrors,
                        lambda m: dry_run_mirror(m, net, mirror_dir, pool),
                        _report_dry,
                    )
                    if net.dist_upgrade:
                        dry_result.dist_upgrade_paths, dry_result.dist_upgrade_warnings = \
                            await asyncio.to_thread(dist_upgrade_info, net, self._cfg, pool)
        finally:
            try:
                self.app.pop_screen()
            except Exception:
                pass
            await asyncio.sleep(0)  # let Textual process the pop before caller pushes next screen

        return verify_results, file_counts, dry_result

    def _incomplete_lines(self, verify_results: list) -> list[str]:
        return [
            f"  • {r.mirror_name}: {r.error}" if r.error
            else f"  • {r.mirror_name}: {r.missing_count:,} of {r.total:,} files missing"
            for r in verify_results if not r.complete
        ]

    def _selected_mirrors_for(self, net: "config.NetworkConfig") -> list:
        mirror_map = {
            m.name: m
            for m in network_mirrors(net, self._cfg, self._cfg.settings.mirror_dir)
        }
        selected_names = list(mirror_map)
        return [mirror_map[n] for n in selected_names if n in mirror_map]

    async def _show_incomplete(self, verify_results: list) -> bool:
        """Show the incomplete-mirrors modal if needed; True if publish must abort."""
        bad = self._incomplete_lines(verify_results)
        if bad:
            await self.app.push_screen_wait(
                InfoModal("Incomplete Mirrors", "Cannot publish — incomplete mirrors:\n" + "\n".join(bad))
            )
            return True
        return False

    async def _execute_publish(self, net: "config.NetworkConfig", per_mirror: dict[str, int]) -> None:
        """Run publish_network behind a PublishProgressModal and notify the outcome."""
        from mmw.core.publisher import publish_network

        mirror_dir = self._cfg.settings.mirror_dir
        prog_modal = PublishProgressModal(
            net.name, total=sum(per_mirror.values()), per_mirror_counts=per_mirror
        )

        async def _run_publish():
            try:
                r = await asyncio.to_thread(
                    publish_network, net, self._cfg, mirror_dir,
                    prog_modal.record, prog_modal.stop_event, prog_modal.notify_mirror_start,
                )
                prog_modal.mark_done(r)
            except Exception as exc:
                prog_modal.mark_error(str(exc) or type(exc).__name__)

        # run_worker, not ensure_future: the publish task is then owned by the
        # widget and cancelled with it, and a crash surfaces rather than being
        # swallowed by an unreferenced future.
        self.run_worker(_run_publish(), name="publish", exit_on_error=False)
        result = await self.app.push_screen_wait(prog_modal)

        if result is None:
            # Stopped by user or error — notification already shown in modal
            return

        pruned_str = f", {result.pruned:,} pruned" if result.pruned else ""
        du_warn_str = (
            f", {len(result.dist_upgrade_warnings)} dist-upgrade warning(s)"
            if result.dist_upgrade_warnings else ""
        )
        if prog_modal.stop_event.is_set():
            self.app.notify(
                f"Publish stopped: {result.linked:,} linked, {result.skipped:,} already current{pruned_str}.",
                severity="warning",
            )
        elif result.errors:
            self.app.notify(
                f"Publish complete with {len(result.errors)} error(s). "
                f"{result.linked:,} linked, {result.skipped:,} already current{pruned_str}{du_warn_str}.",
                severity="warning",
            )
        else:
            sev = "warning" if result.dist_upgrade_warnings else "information"
            self.app.notify(
                f"Published {net.name}: {result.linked:,} linked, {result.skipped:,} already current{pruned_str}{du_warn_str}.",
                severity=sev,
            )
            if net.certs:
                await self._run_certs(net)

    @on(Button.Pressed, "#btn-publish")
    @work(exit_on_error=False)
    async def do_publish(self):
        net = self._selected_network()
        if net is None:
            self.app.notify("Select a network first.", severity="warning")
            return

        selected_mirrors = self._selected_mirrors_for(net)
        try:
            mirror_dir = self._cfg.settings.mirror_dir
            verify_results, per_mirror, _ = await self._run_pre_publish(net, selected_mirrors, mirror_dir)
            if await self._show_incomplete(verify_results):
                return
            await self._execute_publish(net, per_mirror)
        except Exception as e:
            self.app.notify(f"Publish error: {e}", severity="error", timeout=15)

    @on(Button.Pressed, "#btn-dry-run")
    @work(exit_on_error=False)
    async def do_dry_run(self):
        net = self._selected_network()
        if net is None:
            self.app.notify("Select a network first.", severity="warning")
            return

        selected_mirrors = self._selected_mirrors_for(net)
        try:
            mirror_dir = self._cfg.settings.mirror_dir
            verify_results, _, dry_result = await self._run_pre_publish(
                net, selected_mirrors, mirror_dir, include_dry_run=True, include_file_counts=False
            )
            if await self._show_incomplete(verify_results):
                return

            proceed = await self.app.push_screen_wait(DryRunResultModal(net.name, dry_result))
            if not proceed:
                return

            # User confirmed — compute file counts for the progress bar, but
            # skip re-verifying every mirror (we just did).
            _, per_mirror, _ = await self._run_pre_publish(
                net, selected_mirrors, mirror_dir,
                include_file_counts=True, include_verify=False,
            )
            await self._execute_publish(net, per_mirror)
        except Exception as e:
            self.app.notify(f"Publish error: {e}", severity="error", timeout=15)

    async def _run_certs(self, net: "config.NetworkConfig") -> None:
        """Generate key+CSR for net, showing a preview modal then a results modal."""
        from mmw.core.certs import collect_hostnames, generate_certs

        hostnames = collect_hostnames(net, self._cfg)
        if not hostnames:
            self.app.notify("No mirrors configured for this network.", severity="warning")
            return

        if not await self.app.push_screen_wait(CertsModal(net.name, hostnames)):
            return

        try:
            hostnames, key_path, cnf_path, csr_path, md_path = await asyncio.to_thread(
                generate_certs, net, self._cfg
            )
            await self.app.push_screen_wait(
                CertsModal(net.name, hostnames, key_path=key_path, cnf_path=cnf_path, csr_path=csr_path, md_path=md_path)
            )
        except Exception as e:
            await self.app.push_screen_wait(CertsModal(net.name, hostnames, error=str(e)))

    @work(exit_on_error=False)
    async def action_certs(self):
        net = self._selected_network()
        if net is None:
            self.app.notify("Select a network first.", severity="warning")
            return
        await self._run_certs(net)
