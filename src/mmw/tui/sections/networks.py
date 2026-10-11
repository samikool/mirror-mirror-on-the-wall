"""Networks — named downselects of the mirror, and where they are published.

The logic lives in `tui/networks.py`; this page lists networks beside their
detail and turns keys into flows. Publish, dry run and bundle start in the
Publish section (`app.start_transfer`); certs is a `PublishFlows` page flow.
"""
import asyncio

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.markup import escape
from textual.widgets import Label, ListItem, ListView

from mmw.core import config
from mmw.tui import networks
from mmw.tui.modals import DeleteProgressModal
from mmw.tui.network_screens import AddNetworkPage, EditNetworkPage
from mmw.tui.publish_flows import PublishFlows
from mmw.tui.sections.network_detail import NetworkDetail
from mmw.tui.ui.dialog import ConfirmDialog
from mmw.tui.ui.list_detail import ListDetail
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button, safe_id


class NetworksPage(Page):
    TITLE = "Networks"
    SCROLLS = False
    HELP = ("A network is what gets published, and where. Dry run counts what a publish\n"
            "would link without writing; bundle writes a transfer archive for an offline\n"
            "network; certs generates the network's TLS key and CSR (and checks a signed cert).\n"
            "Publish, dry run and bundle run in the Publish section (4) — keep browsing meanwhile.\n"
            "Publish asks what (live, or a snapshot) and where; bundle asks from and to.\n"
            "Snapshots (n) keeps exact network states to bundle to or publish again.")
    BINDINGS = [
        Binding("a", "add", "Add"),
        Binding("e", "edit", "Edit"),
        Binding("d", "remove", "Remove"),
        Binding("p", "verb('publish')", "Publish"),
        Binding("r", "verb('dry_run')", "Dry run"),
        Binding("b", "verb('bundle')", "Bundle"),
        # Off the footer (it must fit 80 columns): a once-per-network job,
        # still a button, and listed by ?.
        Binding("c", "verb('certs')", "Certs", show=False),
        Binding("n", "snapshots", "Snapshots"),
    ]

    def __init__(self):
        super().__init__()
        self._names: list[str] = []
        self._seen_cfg = None
        self._rebuild_lock = asyncio.Lock()
        self._flows = None
        self._seen_transfers = 0

    def compose_body(self) -> ComposeResult:
        yield ListDetail(ListView(id="net-list"), NetworkDetail(), id="net-panes")

    def compose_actions(self) -> ComposeResult:
        yield Button("Add", id="btn-net-add", compact=True)
        yield Button("Edit", id="btn-net-edit", compact=True)
        yield Button("Remove", id="btn-net-remove", variant="error", compact=True)
        yield Button("Snapshots", id="btn-net-snapshots", compact=True)
        yield Button("Publish", id="btn-net-publish", variant="success", compact=True)
        yield Button("Dry run", id="btn-net-dry-run", compact=True)
        yield Button("Bundle", id="btn-net-bundle", compact=True)
        yield Button("Certs", id="btn-net-certs", compact=True)

    async def on_mount(self) -> None:
        # A transfer finishing while this section is the one showing: re-read
        # the detail now, not only on the next visit (on_screen_resume).
        self.app.transfer_finished_signal.subscribe(self, self._transfer_finished)
        await self.refresh_rows()
        self.query_one("#net-list", ListView).focus()

    def _transfer_finished(self, _: object) -> None:
        if self.app.screen is self and self._seen_cfg is self.app.cfg:
            self._seen_transfers = self.app.transfers_finished
            self._show_detail(force=True)      # republished: re-check its figures

    def on_screen_resume(self) -> None:
        super().on_screen_resume()
        if self._seen_transfers != self.app.transfers_finished:
            # Something was published or bundled since: the detail is stale.
            self._seen_transfers = self.app.transfers_finished
            if self._seen_cfg is self.app.cfg:
                self._show_detail(force=True)
        if self._seen_cfg is not None and self._seen_cfg is not self.app.cfg:
            self.run_worker(self.refresh_rows(), group="net-rows")

    # ── list ─────────────────────────────────────────────────────────────────

    def _network(self, name: str | None) -> "config.NetworkConfig | None":
        return next((n for n in self.app.cfg.networks if n.name == name), None)

    def highlighted(self) -> "config.NetworkConfig | None":
        lst = self.query_one("#net-list", ListView)
        if lst.index is None or not (0 <= lst.index < len(self._names)):
            return None
        return self._network(self._names[lst.index])

    async def refresh_rows(self, select: str | None = None) -> None:
        async with self._rebuild_lock:
            lst = self.query_one("#net-list", ListView)
            keep = select or (self._names[lst.index] if lst.index is not None
                              and lst.index < len(self._names) else None)
            self._seen_cfg = self.app.cfg
            self._names = sorted(n.name for n in self.app.cfg.networks)
            await lst.clear()
            await lst.extend([ListItem(Label(escape(n)), id=safe_id(n, prefix="net-"))
                              for n in self._names])
            lst.index = (self._names.index(keep) if keep in self._names
                         else (0 if self._names else None))
            self._show_detail()

    def _show_detail(self, *, force: bool = False) -> None:
        self.query_one(NetworkDetail).show(self.highlighted(), self.app.cfg, force=force)

    @on(ListView.Highlighted, "#net-list")
    def _highlighted(self) -> None:
        self._show_detail()

    # ── actions ──────────────────────────────────────────────────────────────

    def action_add(self) -> None:
        self.start_flow(self._add)

    def action_edit(self) -> None:
        self.start_flow(self._edit)

    def action_remove(self) -> None:
        self.start_flow(self._remove)

    @on(Button.Pressed, "#btn-net-add")
    def _add_pressed(self) -> None:
        self.action_add()

    @on(Button.Pressed, "#btn-net-edit")
    def _edit_pressed(self) -> None:
        self.action_edit()

    @on(Button.Pressed, "#btn-net-remove")
    def _remove_pressed(self) -> None:
        self.action_remove()

    def action_verb(self, verb: str) -> None:
        net = self.highlighted()
        if net is None:
            self.notify("Select a network first.", severity="warning")
            return
        if verb == "certs":
            if self._flows is None:
                self._flows = PublishFlows(self.app, lambda: self.app.cfg)
            self.start_flow(lambda: self._flows.certs(net))
            return
        # Publish, dry run and bundle run in the Publish section, which keeps
        # them going while other sections are browsed.
        self.start_flow(lambda: self._start_transfer(verb, net))

    async def _start_transfer(self, verb: str, net: "config.NetworkConfig") -> None:
        busy = self.app.active_transfer
        if busy is not None and busy.running:
            # Before any question: picking delta/full only to be refused is
            # worse than being refused. (start_transfer checks again.)
            self.notify(f"Already working on {busy.network_name} — see the Publish section.",
                        severity="warning")
            return
        choice = None
        if verb == "bundle" and net.bundle_dir is None:
            self.notify(f"Network '{net.name}' has no bundle directory — set one in Edit.",
                        severity="warning")
            return
        if verb in ("publish", "bundle"):
            # Asked here, where the user is, before the section switches.
            from mmw.tui.snapshot_pages import BundlePickPage, PublishPickPage, load_status
            mirror_dir = self.app.cfg.settings.mirror_dir
            status = await asyncio.to_thread(load_status, mirror_dir, net)
            page = (PublishPickPage(net, status) if verb == "publish"
                    else BundlePickPage(net, status, mirror_dir))
            choice = await self.app.push_screen_wait(page)
            if choice is None:
                return          # cancelled — never falls back to a default silently
        await self.app.start_transfer(verb, net, choice=choice)

    def action_snapshots(self) -> None:
        net = self.highlighted()
        if net is None:
            self.notify("Select a network first.", severity="warning")
            return
        from mmw.tui.snapshot_pages import SnapshotsPage
        self.app.push_screen(SnapshotsPage(net), lambda _: self._show_detail(force=True))

    @on(Button.Pressed, "#btn-net-snapshots")
    def _snapshots_pressed(self) -> None:
        self.action_snapshots()

    @on(Button.Pressed, "#btn-net-publish")
    def _publish_pressed(self) -> None:
        self.action_verb("publish")

    @on(Button.Pressed, "#btn-net-dry-run")
    def _dry_run_pressed(self) -> None:
        self.action_verb("dry_run")

    @on(Button.Pressed, "#btn-net-bundle")
    def _bundle_pressed(self) -> None:
        self.action_verb("bundle")

    @on(Button.Pressed, "#btn-net-certs")
    def _certs_pressed(self) -> None:
        self.action_verb("certs")

    async def _add(self) -> None:
        cfg = self.app.cfg
        available = await asyncio.to_thread(networks.available, cfg)
        if not available:
            # Before the first sync there is nothing to publish; an empty
            # wizard would refuse with "select at least one repository",
            # which blames the user for a list they could not have filled.
            self.notify("No mirrors to publish yet — sync at least one repository first.",
                        severity="warning")
            return
        net = await self.app.push_screen_wait(
            AddNetworkPage(available, {n.name for n in cfg.networks}))
        if net is None:
            return
        try:
            await self.app.save_config(lambda c: networks.with_network(c, net))
        except Exception as e:  # noqa: BLE001 — shown on the page
            self.set_status(f"Not added: {e}", "error")
            return
        await self.refresh_rows(select=net.name)

    async def _edit(self) -> None:
        net = self.highlighted()
        if net is None:
            self.notify("Select a network first.", severity="warning")
            return
        cfg = self.app.cfg
        available = await asyncio.to_thread(networks.available, cfg)
        old_mirrors = await asyncio.to_thread(networks.mirror_names, net, cfg)
        edited = await self.app.push_screen_wait(EditNetworkPage(
            net, available, existing_names={n.name for n in cfg.networks}))
        if edited is None:
            return
        try:
            await self.app.save_config(lambda c: networks.replace_network(c, net.name, edited))
        except Exception as e:  # noqa: BLE001
            self.set_status(f"Not saved: {e}", "error")
            return
        mirror_dir = self.app.cfg.settings.mirror_dir
        # A rename moves the network's certs and bundle ledger with it.
        for problem in await asyncio.to_thread(networks.rename_state, mirror_dir,
                                               net.name, edited.name):
            self.notify(problem, severity="warning", timeout=12)
        await self.refresh_rows(select=edited.name)
        new_mirrors = await asyncio.to_thread(networks.mirror_names, edited, self.app.cfg)
        removed = old_mirrors - new_mirrors
        if removed:
            await self._offer_removed_trees(net, edited, available, removed, new_mirrors)

    async def _offer_removed_trees(self, before, net, available, removed, kept) -> None:
        """Only published trees no kept mirror shares are offered for deletion —
        and only under the root they were published to (`networks.stale_trees`)."""
        mirror_map = {m.name: m for m in available}
        dirs, shared, note = await asyncio.to_thread(
            networks.stale_trees, before, net, mirror_map, removed, kept)
        if note:
            self.notify(note, timeout=12)
        if shared:
            where = ("the next publish prunes them" if net.prune else
                     "prune is off for this network, so they stay until you turn it on")
            self.notify(f"{len(shared)} removed mirror(s) share a published tree with mirrors "
                        f"you kept — those files stay until {where}.", timeout=12)
        if dirs:
            paths = "\n".join(f"  [dim]{escape(str(d))}[/dim]" for d in dirs)
            if await self.app.push_screen_wait(ConfirmDialog(
                    "Delete published files?",
                    f"Delete published files for removed mirror(s)?\n{paths}",
                    confirm_label="Yes", cancel_label="No")):
                await self.app.push_screen_wait(
                    DeleteProgressModal(f"Deleting published files — {net.name}", dirs))

    async def _remove(self) -> None:
        net = self.highlighted()
        if net is None:
            return
        if not await self.app.push_screen_wait(ConfirmDialog(
                "Remove network", f"Remove network [b]{escape(net.name)}[/b]?", "Remove")):
            return
        try:
            await self.app.save_config(lambda c: networks.without_network(c, net.name))
        except Exception as e:  # noqa: BLE001
            self.set_status(f"Not removed: {e}", "error")
            return
        await self.refresh_rows()
        mirror_dir = self.app.cfg.settings.mirror_dir
        problem = await asyncio.to_thread(networks.clear_ledger, mirror_dir, net.name)
        if problem:
            self.notify(problem, severity="warning")
        if net.root is not None and await asyncio.to_thread(net.root.exists):
            if await self.app.push_screen_wait(ConfirmDialog(
                    "Delete published files?",
                    f"Also delete all published files?\n  [dim]{escape(str(net.root))}[/dim]",
                    confirm_label="Yes", cancel_label="No")):
                await self.app.push_screen_wait(
                    DeleteProgressModal(f"Deleting — {net.name}", [net.root]))
