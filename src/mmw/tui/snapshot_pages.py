"""Network snapshots in the TUI: the pickers `p` and `b` open on Networks, and
the Snapshots page (`n`) — list, take, rename, delete, clean up.

Every pool read and write runs off the event loop. Taking a snapshot walks the
whole network (20–50 s on a big one), so it runs as an app-level worker: it
carries on, and reports, when this page is left.
"""
import asyncio
from pathlib import Path

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.markup import escape
from textual.widgets import DataTable, Input, Label, Select

from mmw.core import config
from mmw.core.format import fmt_size
from mmw.core.pool import Pool, SnapshotInfo
from mmw.core.snapshots import SnapshotStatus
from mmw.tui.publish_flows import BundleChoice, PublishChoice
from mmw.tui.ui.dialog import ConfirmDialog
from mmw.tui.ui.form import ChoiceField, Form, PathField, TextField, ToggleField
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


def load_status(mirror_dir: Path, net: config.NetworkConfig) -> SnapshotStatus:
    """**Blocking.** The network's snapshots, with bundle history from before
    snapshots existed brought in first (as `mmw snapshot list` does)."""
    from mmw.core.snapshots import import_ledger, snapshot_status
    with Pool(mirror_dir) as pool:
        import_ledger(pool, mirror_dir, net.name)
        return snapshot_status(pool, net.name, net.root)


def _describe(s: SnapshotInfo) -> str:
    label = f"  {s.label}" if s.label else ""
    return escape(f"{s.stamp}{label}  · {s.kind} · {s.files:,} files")


# ── pickers ──────────────────────────────────────────────────────────────────

class PublishPickPage(Page):
    """What to publish (live or a complete snapshot), and into which directory.
    Dismisses with a `PublishChoice`, or None."""

    def __init__(self, net: config.NetworkConfig, status: SnapshotStatus):
        super().__init__()
        self.TITLE = f"Publish {net.name}"
        self._net = net
        self._status = status

    def compose_body(self) -> ComposeResult:
        targets = [("Live — the current state", "live")] + [
            (_describe(s), s.stamp) for s in self._status.snapshots if s.complete]
        dirs = ([(escape(f"The network's root — {self._net.root}"), "root")]
                if self._net.root is not None else []) + [("Another directory", "other")]
        yield Form(
            ChoiceField("Publish", targets, "live", id="pick-target",
                        hint="A snapshot is laid out exactly as it was — older packages "
                             "come back, newer ones go."),
            ChoiceField("Into", dirs, dirs[0][1], id="pick-dir",
                        hint="Another directory gets its own nginx config, same hostnames — "
                             "serve it on its own address."),
            PathField("Directory", "", id="pick-root", browse_title="Publish into",
                      hint="Created if missing."),
            ToggleField("Recheck every file", False, id="publish-full",
                        hint="Ignore the record of the last publish (--full) — slow; for a "
                             "tree edited by hand."),
            ToggleField("Snapshot first", False, id="publish-take-snapshot",
                        hint="Take a snapshot of the network before publishing "
                             "(--take-snapshot). Publish never takes one on its own."),
            id="pick-form")
        serving = self._status.serving
        if serving is not None:
            yield Label(f"[dim]The root serves {escape(self._status.name(serving))} now.[/]",
                        id="pick-note")

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-pick-cancel", compact=True)
        yield Button("Publish", id="btn-pick-go", variant="primary", compact=True)

    def on_mount(self) -> None:
        self._show_root()
        self.query_one("#btn-pick-go").focus()

    @on(Select.Changed, "#pick-dir")
    def _show_root(self) -> None:
        self.query_one("#field-pick-root").display = (
            self.query_one("#pick-dir", Select).value == "other")

    @on(Button.Pressed, "#btn-pick-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-pick-go")
    def _go(self) -> None:
        values = self.query_one(Form).values()
        root = None
        if values["pick-dir"] == "other":
            if not values["pick-root"]:
                self.set_status("Choose the directory to publish into.", "error")
                self.query_one("#pick-root").focus()
                return
            root = Path(values["pick-root"]).expanduser()
        target = values["pick-target"]
        if values["publish-take-snapshot"] and target != "live":
            self.set_status("Snapshot first: nothing to snapshot — you are publishing an "
                            "existing snapshot.", "error")
            return
        self.dismiss(PublishChoice(snapshot=None if target == "live" else target, root=root,
                                   full=values["publish-full"],
                                   take_snapshot=values["publish-take-snapshot"]))


class BundlePickPage(Page):
    """Where the far side is (From) and where to bring it (To), with what that
    sends. Dismisses with a `BundleChoice`, or None."""

    def __init__(self, net: config.NetworkConfig, status: SnapshotStatus, mirror_dir: Path):
        super().__init__()
        self.TITLE = f"Bundle {net.name}"
        self._net = net
        self._status = status
        self._mirror_dir = mirror_dir
        # Where `bundle_network` starts by default: the last bundle's
        # destination, or before any, the newest imported bundle manifest.
        last = status.last_bundle.to_stamp if status.last_bundle else next(
            (s.stamp for s in status.snapshots if s.kind == "imported"), None)
        self._last = last

    def compose_body(self) -> ComposeResult:
        froms = ([(escape(f"Where the last bundle went — {self._status.name(self._last)}"), "last")]
                 if self._last else [])
        froms += [("Nothing — a full bundle", "none")]
        froms += [(_describe(s), s.stamp) for s in self._status.snapshots]
        tos = [("A new snapshot of now", "now")] + [
            (_describe(s), s.stamp) for s in self._status.snapshots if s.complete]
        yield Form(
            ChoiceField("From", froms, froms[0][1], id="pick-from",
                        hint="The snapshot the far side is at. A full bundle also repairs a "
                             "far side of unknown state."),
            ChoiceField("To", tos, "now", id="pick-to",
                        hint="Backwards is fine: the far side is brought to exactly that state."),
            id="pick-form")
        yield Label("", id="pick-estimate")

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-pick-cancel", compact=True)
        yield Button("Bundle", id="btn-pick-go", variant="primary", compact=True)

    def on_mount(self) -> None:
        self._estimate_now()
        self.query_one("#btn-pick-go").focus()

    def _ends(self) -> tuple[str, str]:
        values = self.query_one(Form).values()
        return values["pick-from"], values["pick-to"]

    @on(Select.Changed)
    def _estimate_now(self) -> None:
        start, dest = self._ends()
        start = self._last if start == "last" else start
        line = self.query_one("#pick-estimate", Label)
        if dest == "now":
            line.update("[dim]What it sends is worked out once the snapshot is taken.[/]")
            return
        if start == dest:
            line.update("[$mmw-warn]From and To are the same snapshot — nothing to send.[/]")
            return
        line.update("[dim]Estimating…[/]")
        self._estimate(None if start == "none" else start, dest)

    @work(thread=True, exclusive=True, exit_on_error=False, group="bundle-estimate")
    def _estimate(self, start: str | None, dest: str) -> None:
        ids = {s.stamp: s.id for s in self._status.snapshots}
        with Pool(self._mirror_dir, read_only=True) as pool:
            files, size = pool.snapshot_delta(ids.get(start) if start else None, ids[dest])
        self.app.call_from_thread(self._estimated, start, dest, files, size)

    def _estimated(self, start, dest, files: int, size: int) -> None:
        now_start, now_dest = self._ends()
        now_start = self._last if now_start == "last" else now_start
        if (None if now_start == "none" else now_start, now_dest) != (start, dest):
            return                                  # the choice moved on
        self.query_one("#pick-estimate", Label).update(
            f"Sends about [b]{files:,}[/] files · [b]{fmt_size(size)}[/]")

    @on(Button.Pressed, "#btn-pick-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-pick-go")
    def _go(self) -> None:
        start, dest = self._ends()
        if dest != "now" and (self._last if start == "last" else start) == dest:
            self.set_status("From and To are the same snapshot — nothing to send.", "error")
            return
        self.dismiss(BundleChoice(from_last=start == "last",
                                  from_ref=None if start in ("last", "none") else start,
                                  to_ref=dest))


# ── the Snapshots page ───────────────────────────────────────────────────────

class LabelPage(Page):
    """A snapshot's label. Dismisses with the text ("" for none), or None."""

    def __init__(self, title: str, label: str = ""):
        super().__init__()
        self.TITLE = title
        self._label = label

    def compose_body(self) -> ComposeResult:
        yield Form(TextField("Label", self._label, id="inp-snap-label",
                             placeholder="e.g. pre-upgrade",
                             hint="Optional — a name to find it by. Enter saves."))

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-label-cancel", compact=True)
        yield Button("Save", id="btn-label-save", variant="primary", compact=True)

    def on_mount(self) -> None:
        self.query_one("#inp-snap-label").focus()

    @on(Button.Pressed, "#btn-label-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#inp-snap-label")
    @on(Button.Pressed, "#btn-label-save")
    def _save(self) -> None:
        self.dismiss(self.query_one("#inp-snap-label", Input).value.strip())


class CleanPage(Page):
    """Keep the newest N; previews what goes and what a prune then frees.
    Dismisses with N, or None."""

    def __init__(self, net: config.NetworkConfig, mirror_dir: Path):
        super().__init__()
        self.TITLE = "Clean up"
        self._net = net
        self._mirror_dir = mirror_dir

    def compose_body(self) -> ComposeResult:
        yield Form(TextField("Keep newest", "3", id="inp-snap-keep", integer_min=0,
                             hint="Snapshots a directory serves, and the one the last "
                                  "bundle went to, are always kept."))
        yield Label("", id="clean-preview")

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-clean-cancel", compact=True)
        yield Button("Delete", id="btn-clean-go", variant="error", compact=True)

    def on_mount(self) -> None:
        self.query_one("#inp-snap-keep").focus()
        self._changed()

    def _keep(self) -> int | None:
        field = self.query_one(TextField)
        return None if field.validate() else field.value

    @on(Input.Changed, "#inp-snap-keep")
    def _changed(self) -> None:
        keep = self._keep()
        if keep is None:
            self.query_one("#clean-preview", Label).update("[dim]A whole number, 0 or more.[/]")
            return
        self.query_one("#clean-preview", Label).update("[dim]Working it out…[/]")
        self._preview(keep)

    @work(thread=True, exclusive=True, exit_on_error=False, group="clean-preview")
    def _preview(self, keep: int) -> None:
        from mmw.core.snapshots import clean_plan
        with Pool(self._mirror_dir, read_only=True) as pool:
            plan = clean_plan(pool, self._net.name, keep)
        self.app.call_from_thread(self._previewed, keep, plan)

    def _previewed(self, keep: int, plan) -> None:
        if self._keep() != keep:
            return
        if not plan.delete:
            text = "Nothing to delete."
        else:
            text = (f"Deletes [b]{len(plan.delete)}[/] snapshot(s); the next prune frees "
                    f"[b]{fmt_size(plan.frees)}[/].")
        if plan.kept:
            text += f"\n[dim]Keeps {len(plan.kept)} older: " + escape(", ".join(
                f"{s.stamp} ({why})" for s, why in plan.kept)) + "[/]"
        self.query_one("#clean-preview", Label).update(text)

    @on(Button.Pressed, "#btn-clean-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#inp-snap-keep")
    @on(Button.Pressed, "#btn-clean-go")
    def _go(self) -> None:
        keep = self._keep()
        if keep is None:
            self.set_status("Keep must be a whole number, 0 or more.", "error")
            return
        self.dismiss(keep)


class SnapshotsPage(Page):
    """One network's snapshots, newest first."""

    SCROLLS = False
    HELP = ("A snapshot is the network's exact published state, kept in the pool to bundle\n"
            "to or publish again (p on Networks offers them). Holds: the disk only that\n"
            "snapshot keeps — freed by the next prune once it is deleted. A snapshot a\n"
            "directory serves cannot be deleted; history-only ones (from old bundles) can\n"
            "only be a bundle's starting point.")
    BINDINGS = [
        Binding("t", "take", "Take"),
        Binding("r", "rename", "Rename"),
        Binding("d", "delete", "Delete"),
        Binding("c", "clean", "Clean up"),
    ]

    def __init__(self, net: config.NetworkConfig):
        super().__init__()
        self.TITLE = "Snapshots"
        self.subtitle = net.name
        self._net = net
        self._status: SnapshotStatus | None = None

    @property
    def _mirror_dir(self) -> Path:
        return self.app.cfg.settings.mirror_dir

    def compose_body(self) -> ComposeResult:
        yield DataTable(cursor_type="row", zebra_stripes=True, id="snap-table")

    def compose_actions(self) -> ComposeResult:
        yield Button("Take", id="btn-snap-take", variant="primary", compact=True)
        yield Button("Rename", id="btn-snap-rename", compact=True)
        yield Button("Delete", id="btn-snap-delete", variant="error", compact=True)
        yield Button("Clean up…", id="btn-snap-clean", compact=True)

    def on_mount(self) -> None:
        table = self.query_one("#snap-table", DataTable)
        table.add_columns("Snapshot", "Label", "Kind", "Files", "Holds", "")
        table.focus()
        self.reload()

    # ── loading ──────────────────────────────────────────────────────────────

    def reload(self) -> None:
        if self._net.name in _taking(self.app):
            self.set_status("Taking a snapshot…")
        self._load()

    @work(thread=True, exclusive=True, exit_on_error=False, group="snapshots-load")
    def _load(self) -> None:
        try:
            status = load_status(self._mirror_dir, self._net)
        except Exception as e:  # noqa: BLE001 — shown on the page
            self.app.call_from_thread(self.set_status, f"Couldn't read snapshots: {e}", "error")
            return
        self.app.call_from_thread(self._loaded, status)
        # What each alone holds: a query per snapshot — filled in after.
        holds = {}
        with Pool(self._mirror_dir, read_only=True) as pool:
            for s in status.snapshots:
                holds[s.stamp] = pool.snapshot_exclusive_bytes(s.id)
        self.app.call_from_thread(self._holds, status, holds)

    def _note(self, s: SnapshotInfo, status: SnapshotStatus) -> str:
        notes = []
        if status.serving == s.stamp:
            notes.append("published")
        if status.last_bundle and status.last_bundle.to_stamp == s.stamp:
            notes.append("last bundled")
        if not s.complete:
            notes.append("history only")
        return " · ".join(notes)

    def _loaded(self, status: SnapshotStatus) -> None:
        if not self.is_attached:
            return                                  # left while it loaded
        self._status = status
        table = self.query_one("#snap-table", DataTable)
        keep = self._selected()
        table.clear()
        for s in status.snapshots:
            # Text, not str: a str cell is parsed as markup, and a label is user text.
            table.add_row(s.stamp, Text(s.label or ""), s.kind, f"{s.files:,}", "…",
                          self._note(s, status), key=s.stamp)
        if keep is not None and keep.stamp in {s.stamp for s in status.snapshots}:
            table.move_cursor(row=table.get_row_index(keep.stamp))
        if not status.snapshots:
            self.set_status(f"No snapshots of {self._net.name} yet — t takes one.")
        elif self._net.name not in _taking(self.app):
            self.set_status(f"{len(status.snapshots)} snapshot(s).")

    def _holds(self, status: SnapshotStatus, holds: dict[str, int]) -> None:
        if status is not self._status or not self.is_attached:
            return
        table = self.query_one("#snap-table", DataTable)
        column = list(table.columns)[4]
        for stamp, size in holds.items():
            table.update_cell(stamp, column, fmt_size(size))

    def _selected(self) -> SnapshotInfo | None:
        if self._status is None or not self._status.snapshots:
            return None
        row = self.query_one("#snap-table", DataTable).cursor_row
        snaps = self._status.snapshots
        return snaps[row] if 0 <= row < len(snaps) else None

    # ── actions ──────────────────────────────────────────────────────────────

    def action_take(self) -> None:
        self.start_flow(self._take)

    def action_rename(self) -> None:
        self.start_flow(self._rename)

    def action_delete(self) -> None:
        self.start_flow(self._delete)

    def action_clean(self) -> None:
        self.start_flow(self._clean)

    @on(Button.Pressed, "#btn-snap-take")
    def _take_pressed(self) -> None:
        self.action_take()

    @on(Button.Pressed, "#btn-snap-rename")
    def _rename_pressed(self) -> None:
        self.action_rename()

    @on(Button.Pressed, "#btn-snap-delete")
    def _delete_pressed(self) -> None:
        self.action_delete()

    @on(Button.Pressed, "#btn-snap-clean")
    def _clean_pressed(self) -> None:
        self.action_clean()

    async def _take(self) -> None:
        net, app = self._net, self.app
        if net.name in _taking(app):
            self.notify(f"Already taking a snapshot of {net.name}.", severity="warning")
            return
        label = await app.push_screen_wait(LabelPage(f"Take a snapshot of {net.name}"))
        if label is None:
            return
        _taking(app).add(net.name)
        self.set_status("Taking a snapshot…")
        app.run_worker(_take_in_background(app, net, label or None, self),
                       group="snapshot-take", exit_on_error=False)

    async def _rename(self) -> None:
        snap = self._selected()
        if snap is None:
            return
        label = await self.app.push_screen_wait(
            LabelPage(f"Rename {snap.stamp}", snap.label or ""))
        if label is None:
            return

        def rename() -> None:
            with Pool(self._mirror_dir) as pool:
                pool.rename_snapshot(snap.id, label or None)
        await asyncio.to_thread(rename)
        self.reload()

    async def _delete(self) -> None:
        from mmw.core.snapshots import delete_snapshot, served_by
        snap = self._selected()
        if snap is None or self._status is None:
            return
        name = escape(snap.stamp + (f" ({snap.label})" if snap.label else ""))

        def where() -> list[str]:
            with Pool(self._mirror_dir, read_only=True) as pool:
                return served_by(pool, snap)
        roots = await asyncio.to_thread(where)
        if roots:
            self.notify(f"{snap.stamp} is published at {', '.join(roots)} — publish something "
                        "else there first.", severity="warning", timeout=12)
            return
        last = self._status.last_bundle
        message = f"Delete snapshot [b]{name}[/]? The next prune frees the files only it holds."
        if last is not None and last.to_stamp == snap.stamp:
            message += ("\n[$mmw-warn]The last bundle went to it: the next bundle then needs "
                        "a From chosen (or a full one).[/]")
        if not await self.app.push_screen_wait(ConfirmDialog("Delete snapshot", message,
                                                             "Delete")):
            return

        def delete() -> None:
            with Pool(self._mirror_dir) as pool:
                delete_snapshot(pool, snap)
        try:
            await asyncio.to_thread(delete)
        except ValueError as e:                     # published meanwhile
            self.notify(str(e), severity="warning", timeout=12)
        self.reload()

    async def _clean(self) -> None:
        from mmw.core.snapshots import clean_plan, delete_snapshots
        keep = await self.app.push_screen_wait(CleanPage(self._net, self._mirror_dir))
        if keep is None:
            return

        def clean() -> int:
            with Pool(self._mirror_dir) as pool:
                plan = clean_plan(pool, self._net.name, keep)
                delete_snapshots(pool, plan.delete)
                return len(plan.delete)
        n = await asyncio.to_thread(clean)
        self.notify(f"Deleted {n} snapshot(s) of {self._net.name} — the next prune frees "
                    "their files." if n else "Nothing to delete.")
        self.reload()


def _taking(app) -> set[str]:
    """Networks a snapshot is being taken of, app-wide: the work outlives
    the page that started it."""
    if not hasattr(app, "snapshots_taking"):
        app.snapshots_taking = set()
    return app.snapshots_taking


async def _take_in_background(app, net: config.NetworkConfig, label: str | None,
                              page: SnapshotsPage) -> None:
    from mmw.core.snapshots import import_ledger, take_snapshot
    mirror_dir = app.cfg.settings.mirror_dir
    warnings: list[str] = []

    def take() -> SnapshotInfo:
        with Pool(mirror_dir) as pool:
            import_ledger(pool, mirror_dir, net.name)
        return take_snapshot(net, app.cfg, mirror_dir, label=label, warnings=warnings)
    try:
        snap = await asyncio.to_thread(take)
    except Exception as e:  # noqa: BLE001 — reported wherever the user is
        app.notify(f"Snapshot of {net.name} not taken: {e}", severity="error", timeout=20)
    else:
        app.notify(f"Snapshot of {net.name} taken: {snap.stamp}"
                   + (f" ({snap.label})" if snap.label else "")
                   + f" · {snap.files:,} files", timeout=10)
        for w in warnings:
            app.notify(w, severity="warning", timeout=15)
    finally:
        _taking(app).discard(net.name)
    if page.is_attached:
        page.reload()
