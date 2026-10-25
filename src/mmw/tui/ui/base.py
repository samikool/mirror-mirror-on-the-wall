"""`ThemedApp` — an App wired to mmw's stylesheet and themes.

`MmwApp` is one; component tests use it directly so they render with the real
stylesheet and real colour roles rather than Textual's defaults.
"""
from pathlib import Path

from textual.app import App

from mmw.core import config
from mmw.core.config import DEFAULT_THEME
from mmw.tui.theme import MMW_THEMES, THEMES_DIRNAME, load_custom_themes, role_defaults

STYLESHEET = Path(__file__).resolve().parent.parent / "mmw.tcss"


class ThemedApp(App):
    CSS_PATH = str(STYLESHEET)
    HAS_SECTIONS = False    # MmwApp: pages show the section sidebar
    # The running SyncView, if any (it registers itself). Quitting asks first
    # while one runs, then stops it so workers finish before the pool closes.
    # The running TransferView (publish / bundle), likewise.
    active_transfer = None
    active_sync = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for theme in MMW_THEMES:
            self.register_theme(theme)
        self.theme = DEFAULT_THEME

    def get_theme_variable_defaults(self) -> dict[str, str]:
        # Re-evaluated on every theme change, so the roles follow the theme.
        return role_defaults(self.current_theme)

    def apply_config_theme(self, cfg: config.Config, cfg_dir: Path) -> list[str]:
        """Register `cfg_dir/themes/*.toml` and apply the configured theme.

        Returns the notes to show the user. Reads files, so call it from
        __init__ (before the event loop exists), never from a handler."""
        custom, problems = load_custom_themes(cfg_dir / THEMES_DIRNAME)
        for theme in custom:
            self.register_theme(theme)
        notes = [f"Theme file skipped — {p}" for p in problems]
        wanted = cfg.settings.theme
        if wanted in self.available_themes:
            self.theme = wanted
        else:
            notes.append(f"Theme {wanted!r} not found — showing {DEFAULT_THEME!r}. "
                         "mmw.toml is unchanged.")
        return notes

    async def action_quit(self) -> None:
        running = [job for job in (self.active_sync, self.active_transfer)
                   if job is not None and job.running]
        if not running:
            self.exit()
            return
        from mmw.tui.ui.dialog import ConfirmDialog

        def answer(yes: bool | None) -> None:
            if yes:
                for job in running:
                    job.stop()
                self.exit()
        what = []
        if self.active_sync in running:
            what.append("A sync is still running: downloads stop, files already in the pool "
                        "are kept, and the next sync picks up where this one left off.")
        transfer = self.active_transfer
        if transfer in running and getattr(transfer, "awaiting_decision", False):
            what.append(f"The dry run of {transfer.network_name} is waiting for Publish now "
                        "or Discard — quitting discards it; nothing is published.")
        elif transfer in running:
            what.append("A publish or transfer is still running: it stops after the current "
                        "file, and what is already written stays — but a bundle already "
                        "writing its archive finishes the archive first.")
        self.push_screen(ConfirmDialog("Quit now?", "\n".join(what), "Quit", "Keep going"),
                         answer)
