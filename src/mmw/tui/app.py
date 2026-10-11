"""`MmwApp` — the one TUI. Sections are modes; see docs/history/tui-overhaul.md."""
import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

from textual.app import ScreenStackError
from textual.binding import Binding
from textual.theme import Theme

from mmw.core import config
from mmw.tui.navigation import SECTION_MODES, SECTIONS
from mmw.tui.sections.networks import NetworksPage
from mmw.tui.sections.repositories import RepositoriesPage
from mmw.tui.sections.settings import SettingsPage
from mmw.tui.sections.setup import SetupPage
from mmw.tui.sections.sync import SyncSection
from mmw.tui.ui.base import ThemedApp


class MmwApp(ThemedApp):
    TITLE = "mmw"
    MODES = {
        "repositories": RepositoriesPage,
        "networks": NetworksPage,
        "sync": SyncSection,
        "settings": SettingsPage,
        "setup": SetupPage,
    }
    BINDINGS = [
        *(Binding(s.key, f"section('{s.mode}')", s.label, show=False) for s in SECTIONS),
        Binding("q", "quit", "Quit", show=False),
        Binding("question_mark", "help", "Help"),
    ]

    def __init__(self, cfg: "config.Config | None", cfg_dir: Path, *,
                 initial_section: str = "repositories", mirror_dir: Path | None = None,
                 max_connections: int | None = None,
                 start_sync_with: "tuple[list[config.MirrorConfig], bool] | None" = None):
        if initial_section not in (*SECTION_MODES, "setup"):
            raise ValueError(f"unknown section {initial_section!r} "
                             f"(one of: {', '.join(SECTION_MODES)})")
        super().__init__()
        # No config yet: the app opens on Setup and carries on from there.
        self._initial_section = "setup" if cfg is None else initial_section
        # `mmw sync`: mirrors already resolved (and reported) by the CLI, and
        # whether it is a dry run. Started once the Sync section is showing.
        self._start_sync_with = start_sync_with
        self.setup_defaults = {"mirror_dir": mirror_dir or Path("./mirror"),
                               "max_connections": max_connections}
        # Every mmw.toml write goes through this, and each builds its config
        # from the current one inside it — otherwise a Save and a theme
        # change in flight together overwrite each other's field.
        self._save_lock = asyncio.Lock()
        self.cfg = cfg
        self.cfg_dir = cfg_dir
        self._startup_notes = self.apply_config_theme(cfg, cfg_dir) if cfg is not None else []

    @property
    def HAS_SECTIONS(self) -> bool:      # noqa: N802 — same name the pages read on ThemedApp
        """The sidebar exists once there is a config, and not during Setup."""
        return self.cfg is not None and self._current_mode != "setup"

    async def on_mount(self) -> None:
        await self.switch_mode(self._initial_section)
        if self._start_sync_with is not None and self.cfg is not None:
            mirrors, dry_run = self._start_sync_with
            await self.screen.start(self.cfg, mirrors, dry_run=dry_run)
        for note in self._startup_notes:
            self.notify(note, severity="warning", timeout=10)
        # Subscribed only now: the theme applied in __init__ — including a
        # fallback — is not a choice the user made, so it is never saved.
        self.theme_changed_signal.subscribe(self, self._theme_changed)
        self.set_interval(0.5, self._sync_label)

    def _theme_changed(self, theme: Theme) -> None:
        # Not exclusive: cancelling a worker does not stop a write already
        # handed to a thread, so queue behind the lock instead.
        self.run_worker(self._persist_theme(), group="theme")

    async def _persist_theme(self) -> None:
        try:
            # `self.theme` is read inside the lock: after quick successive
            # changes the last one is what lands, whichever worker runs first.
            await self._serialised(lambda: self._write_settings(theme=self.theme))
        except Exception as e:  # noqa: BLE001 — shown, not raised: the theme still applies
            self.notify(f"Theme applied but not saved: {e}", severity="error")

    async def save_settings(self, **changes: object) -> None:
        """Write these `[settings]` fields to mmw.toml, then adopt them.

        Only the named fields change; everything else is taken from the
        current config at write time. On failure nothing changes."""
        await self._serialised(lambda: self._write_settings(**changes))

    async def save_config(self, change: Callable[[config.Config], config.Config]) -> None:
        """The one way MmwApp writes mmw.toml. `change` maps the *current*
        config to the new one, inside the lock, so concurrent writers compose
        instead of overwriting each other. On failure nothing changes."""
        await self._serialised(lambda: self._write_config(change))

    async def _serialised(self, write: Callable[[], Awaitable[None]]) -> None:
        """Run `write` holding the save lock, shielded from cancellation.

        A cancelled caller must not leave the file written and `self.cfg`
        stale — the next write would build from the stale copy and silently
        undo this one — nor release the lock while a thread is still writing.
        So the whole lock-write-adopt step finishes regardless."""
        async def run() -> None:
            async with self._save_lock:
                await write()
        await asyncio.shield(run())

    async def _write_config(self, change: Callable[[config.Config], config.Config]) -> None:
        """Caller holds `_save_lock`."""
        candidate = change(self.cfg)
        if candidate == self.cfg:
            return
        await asyncio.to_thread(config.save, candidate, self.cfg_dir)
        self.cfg = candidate

    async def _write_settings(self, **changes: object) -> None:
        """Caller holds `_save_lock`."""
        await self._write_config(lambda c: c.model_copy(
            update={"settings": c.settings.model_copy(update=changes)}))

    async def action_section(self, mode: str) -> None:
        if self.cfg is None:
            return              # nothing to show until Setup has written a config
        await self.switch_mode(mode)

    def action_help(self) -> None:
        from mmw.tui.ui.dialog import HelpDialog

        screen = self.screen
        keys: dict[str, str] = {}
        for active in screen.active_bindings.values():
            binding = active.binding
            if binding.description and active.enabled and binding.action != "help":
                keys.setdefault(self.get_key_display(binding), binding.description)
        title = getattr(screen, "TITLE", None) or "mmw"
        self.push_screen(HelpDialog(title, list(keys.items()), getattr(screen, "HELP", "")))

    # ── setup / re-initialise ───────────────────────────────────────────────

    async def _fresh_modes(self, *modes: str) -> None:
        """Rebuild these sections from scratch on next visit (they hold state
        derived from the previous config)."""
        for mode in modes:
            if mode == self.current_mode:
                continue
            if mode in self._modes:
                await self.remove_mode(mode)
            self.add_mode(mode, self.MODES[mode])

    async def finish_setup(self, cfg: config.Config) -> None:
        self.cfg = cfg
        await self._fresh_modes("repositories", "networks", "settings")
        # Not refreshing "setup" here: this runs inside SetupPage's own
        # handler, and awaiting that screen's removal would deadlock.
        # reinitialise() rebuilds Setup fresh before showing it.
        await self.switch_mode("repositories")

    async def reinitialise(self) -> None:
        """Delete mmw.toml and run Setup. Downloaded packages are kept."""
        if self.active_sync is not None and (self.active_sync.running
                                             or self.active_sync.draining):
            self.notify("Stop the running sync before re-initialising.", severity="warning")
            return
        if any(w.group == "page-flow" and w.is_running for w in self.workers):
            # Re-init tears every section down; a prune or a resolving sync
            # on one of them would be cut off mid-way with no one to report it.
            self.notify("Another action is still running — let it finish, then re-initialise.",
                        severity="warning")
            return
        try:
            await asyncio.to_thread(config.delete, self.cfg_dir)
        except OSError as e:
            self.notify(f"Could not delete mmw.toml: {e}", severity="error", timeout=15)
            return
        # Setup proposes the pool already in use: `./mirror` there would point
        # mmw at an empty pool and download everything again.
        self.setup_defaults = {"mirror_dir": self.cfg.settings.mirror_dir,
                               "max_connections": self.cfg.settings.max_connections}
        self.cfg = None
        await self._fresh_modes("setup")
        await self.switch_mode("setup")
        await self._fresh_modes("repositories", "networks", "settings")

    # ── sync ────────────────────────────────────────────────────────────────

    async def start_sync(self, selection) -> None:
        """Resolve `selection` (off the loop — it lists upstream), then run it
        in the Sync section. One session at a time; a selection that resolves
        to nothing is reported, never widened to everything."""
        from mmw.tui.repositories import resolve_selection

        if self.active_sync is not None and self.active_sync.running:
            self.notify("A sync is already running — see the Sync section.", severity="warning")
            return
        if self.active_sync is not None and self.active_sync.draining:
            # Stopped, but its downloads in flight are still finishing and the
            # pool is not closed yet: a second session would open the same
            # database beside workers still writing through the first.
            self.notify("The last sync is still finishing its downloads — try again in a "
                        "moment.", severity="warning")
            return
        if not self.cfg.sources:
            self.notify("No repositories configured — press a in Repositories to add one.",
                        severity="warning")
            return
        self.notify("Resolving what to sync…")
        mirrors, messages = await asyncio.to_thread(resolve_selection, self.cfg, selection)
        if self.cfg is None:
            return              # re-initialised while resolving: there is no config to sync
        for message in messages:
            self.notify(message, timeout=10)
        if not mirrors:
            return
        await self.switch_mode("sync")
        await self.screen.start(self.cfg, mirrors)

    def _sync_label(self) -> None:
        """The sidebar's Sync link carries progress while a sync runs."""
        sync = self.active_sync
        text = "3 Sync"
        if sync is not None and sync.running:
            done, total = sync.progress()
            text = f"3 Sync ▸ {done}/{total}"
        try:
            screen = self.screen
        except ScreenStackError:
            return      # mid switch_mode: the new mode's stack is not populated yet
        for link in screen.query("#nav-sync"):
            link.update(text)
