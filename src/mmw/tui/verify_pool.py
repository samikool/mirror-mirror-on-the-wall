"""Verify pool — the TUI's `mmw verify` (`--stat`, `--hash`, `--repair`, `-n`).

Both call `commands.verify.verify_pool` and `repair_pool`; this page checks
first and repairs on a second, confirmed press, where the CLI's `--repair`
does both in one run."""
import asyncio
import threading

from textual import on
from textual.app import ComposeResult
from textual.widgets import Select, Static

from mmw.commands.verify import damaged_blobs, repair_pool, verify_pool
from mmw.core.format import fmt_size
from mmw.core.pool import PoolBusy
from mmw.tui.ui.dialog import ConfirmDialog
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button

ALL = "__all__"
SHOWN = 5                     # damaged files listed per mirror, as the CLI does


def describe(results) -> str:
    """The report both doors print, as one block of text."""
    if not results:
        return "No mirrors to verify."
    lines = []
    for r in results:
        if r.error:
            lines.append(f"✗ {r.mirror_name}: {r.error}")
        elif r.complete:
            lines.append(f"✓ {r.mirror_name}: complete · {r.total:,} files")
        else:
            lines.append(f"✗ {r.mirror_name}: {r.describe()}")
        lines += [f"    {line}" for line in r.corrupt[:SHOWN]]
        if len(r.corrupt) > SHOWN:
            lines.append(f"    … and {len(r.corrupt) - SHOWN:,} more")
    complete = sum(1 for r in results if r.complete)
    lines.append(f"\n{complete}/{len(results)} mirror(s) complete.")
    damaged = damaged_blobs(results)
    if damaged:
        lines.append(f"{len(damaged):,} damaged blob(s) — Repair drops them so the next sync "
                     "downloads them again.")
    return "\n".join(lines)


class VerifyPoolPage(Page):
    TITLE = "Verify pool"
    HELP = ("Check refs: every file the mirrors want is in the pool database (fast).\n"
            "Stat blobs: also checks each file is on disk at its size (one stat per file).\n"
            "Re-hash: also re-reads and hashes every file (reads the whole pool).\n"
            "Repair drops what a check found damaged; then sync to download it again.\n"
            "CLI: mmw verify [--stat | --hash] [--repair] [-n NETWORK]")

    def __init__(self, cfg):
        super().__init__()
        self._cfg = cfg
        self._results = None
        self._stop = threading.Event()

    def compose_body(self) -> ComposeResult:
        yield Static("Check the pool holds every file its mirrors want. Checks are read-only; "
                     "Repair is the one write.", markup=False)
        options = [("Every mirror in the pool", ALL)] + [(f"Network {n.name}", n.name)
                                                          for n in self._cfg.networks]
        yield Select(options, value=ALL, allow_blank=False, id="verify-network")
        yield Static("", id="verify-result", markup=False)

    def compose_actions(self) -> ComposeResult:
        yield Button("Check refs", id="btn-verify-refs", compact=True)
        yield Button("Stat blobs", id="btn-verify-stat", compact=True)
        yield Button("Re-hash", id="btn-verify-hash", compact=True)
        yield Button("Repair", id="btn-verify-repair", variant="error", compact=True, disabled=True)
        yield Button("Sync now", id="btn-verify-sync", variant="primary", compact=True, disabled=True)

    def on_unmount(self) -> None:
        self._stop.set()                  # leaving ends a blob scan in flight

    def _network(self) -> str | None:
        value = self.query_one("#verify-network", Select).value
        return None if value in (ALL, Select.BLANK) else value

    @on(Button.Pressed, "#btn-verify-refs")
    def _refs(self, event) -> None:
        event.stop()
        self.start_flow(lambda: self._check(stat_blobs=False, hash_blobs=False))

    @on(Button.Pressed, "#btn-verify-stat")
    def _stat(self, event) -> None:
        event.stop()
        self.start_flow(lambda: self._check(stat_blobs=True, hash_blobs=False))

    @on(Button.Pressed, "#btn-verify-hash")
    def _hash(self, event) -> None:
        event.stop()
        self.start_flow(lambda: self._check(stat_blobs=True, hash_blobs=True))

    @on(Button.Pressed, "#btn-verify-repair")
    def _repair_pressed(self, event) -> None:
        event.stop()
        self.start_flow(self._repair)

    @on(Button.Pressed, "#btn-verify-sync")
    def _sync_pressed(self, event) -> None:
        event.stop()
        self.dismiss("sync")

    def _say(self, text: str) -> None:
        self.app.call_from_thread(self.set_status, text.strip())

    def _show(self, text: str) -> None:
        self.query_one("#verify-result", Static).update(text)

    async def _check(self, *, stat_blobs: bool, hash_blobs: bool) -> None:
        self._results = None
        self.query_one("#btn-verify-repair", Button).disabled = True
        self._show("")
        self.set_status("Checking the pool…")
        try:
            results = await asyncio.to_thread(verify_pool, self._cfg, self._network(),
                                              stat_blobs=stat_blobs, hash_blobs=hash_blobs,
                                              say=self._say, stop_event=self._stop)
        except (OSError, LookupError, ValueError) as e:
            self.set_status(f"Cannot read the pool: {e}", "error")
            return
        if not self.is_mounted:
            return
        self._results = results
        self._show(describe(results))
        damaged = damaged_blobs(results)
        self.query_one("#btn-verify-repair", Button).disabled = not damaged
        if all(r.complete for r in results):
            self.set_status("Every mirror is complete.", "ok")
        elif damaged:
            self.set_status(f"{len(damaged):,} damaged blob(s) — press Repair.", "error")
        elif not (stat_blobs or hash_blobs):
            self.set_status("Incomplete — Stat blobs also finds files gone from disk.", "error")
        else:
            self.set_status("Incomplete — sync to fetch what is missing.", "error")

    async def _repair(self) -> None:
        damaged = damaged_blobs(self._results or [])
        if not damaged:
            return
        if not await self.app.push_screen_wait(ConfirmDialog(
                "Repair the pool",
                f"Drop {len(damaged):,} damaged blob(s) from the pool?\n"
                "The next sync downloads them again; until then the mirrors stay incomplete.",
                "Repair")):
            return
        self.set_status("Repairing…")
        try:
            count, size = await asyncio.to_thread(repair_pool, self._cfg, self._results)
        except (PoolBusy, OSError) as e:
            self.set_status(str(e), "error")
            return
        self._results = None
        self.query_one("#btn-verify-repair", Button).disabled = True
        self.query_one("#btn-verify-sync", Button).disabled = False
        self.set_status(f"Dropped {count:,} damaged blob(s) ({fmt_size(size)}) — "
                        "press Sync now to download them again.", "ok")
