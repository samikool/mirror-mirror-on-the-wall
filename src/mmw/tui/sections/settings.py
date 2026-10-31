"""Settings — the `[settings]` table of mmw.toml.

The theme applies and is saved the moment it is picked, exactly like the
command palette's theme switcher (the app persists every theme change). The
pool directory and download count apply on Save, because a half-typed path
must never reach mmw.toml.
"""
import asyncio
from pathlib import Path

from textual import on
from textual.markup import escape
from textual.app import ComposeResult
from textual.widgets import Input, Label, Select

from mmw.tui.theme import ordered_theme_names
from mmw.tui.ui.dialog import ConfirmDialog
from mmw.tui.ui.form import ActionField, ChoiceField, Form, PathField, TextField, ToggleField
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


class SettingsPage(Page):
    TITLE = "Settings"

    def compose_body(self) -> ComposeResult:
        s = self.app.cfg.settings
        names = ordered_theme_names(self.app.available_themes)
        yield Form(
            PathField(
                "Pool directory", str(s.mirror_dir), id="settings-mirror-dir",
                required=True, browse_title="Choose the pool directory",
                hint="Where the pool, keys and certs live. Changing it moves nothing — move "
                     "the directory yourself. A database at its default place follows the "
                     "directory; one moved elsewhere (Database) stays put.",
            ),
            ActionField(
                "Database", str(s.database), "Move…", id="settings-database",
                summary_id="settings-database-path",
                hint="The pool's index. On a fast disk, publish and verify stop waiting on "
                     "the pool disk. Move… copies it there; edited by hand, move it yourself.",
            ),
            TextField(
                "Parallel downloads", str(s.max_connections),
                id="settings-max-connections", integer_min=1,
                hint="Simultaneous downloads across all mirrors.",
            ),
            TextField(
                "Disk link threads", str(s.disk_write_threads),
                id="settings-disk-write-threads", integer_min=1,
                hint="Hardlinks and deletes in flight per job. 8 measured best on one HDD; "
                     "1 is one at a time.",
            ),
            TextField(
                "Disk read threads", str(s.disk_read_threads),
                id="settings-disk-read-threads", integer_min=1,
                hint="Stats and small reads in flight per job. 16 measured best on one HDD; "
                     "1 is one at a time.",
            ),
            TextField(
                "Disk read-ahead (files)", str(s.disk_readahead),
                id="settings-disk-readahead", integer_min=0,
                hint="Files opened ahead of a bundle or hash stream. 0 turns it off.",
            ),
            ToggleField(
                "Check for mmw updates", s.update_check, id="settings-update-check",
                hint="On every start, ask PyPI for a newer mmw and offer it. "
                     "MMW_NO_UPDATE_CHECK=1 also turns it off (for cron and CI).",
            ),
            ActionField(
                "Signing keys", "the keys your own repositories are signed with", "Manage…",
                id="settings-signing-keys", summary_id="settings-signing-keys-summary",
                hint="Generate, import, export or delete the keys mmw signs folder "
                     "repositories with (`mmw signing-key`).",
            ),
            ChoiceField(
                "Theme", [(n, n) for n in names], self.app.theme, id="settings-theme",
                hint="Applies and saves at once. Custom themes: themes/*.toml beside mmw.toml "
                     "(see docs/themes.md).",
            ),
        )

    def compose_actions(self) -> ComposeResult:
        yield Button("Re-initialize…", id="btn-settings-reinit", variant="warning", compact=True)
        yield Button("Revert", id="btn-settings-revert", compact=True)
        yield Button("Save", id="btn-settings-save", variant="primary", compact=True)

    def on_screen_resume(self) -> None:
        super().on_screen_resume()
        picker = self.query_one("#settings-theme", Select)
        if picker.value != self.app.theme:
            picker.value = self.app.theme

    @on(Select.Changed, "#settings-theme")
    def _theme_picked(self, event: Select.Changed) -> None:
        if event.value != Select.BLANK and event.value != self.app.theme:
            self.app.theme = event.value

    @on(Button.Pressed, "#btn-settings-revert")
    def _revert(self) -> None:
        s = self.app.cfg.settings
        self.query_one("#settings-mirror-dir", Input).value = str(s.mirror_dir)
        self.query_one("#settings-max-connections", Input).value = str(s.max_connections)
        for key in ("disk_write_threads", "disk_read_threads", "disk_readahead"):
            self.query_one(f"#settings-{key.replace('_', '-')}", Input).value = str(getattr(s, key))
        self.set_status("Reverted to the saved values.")

    @on(Button.Pressed, "#btn-settings-save")
    async def _save(self) -> None:
        form = self.query_one(Form)
        if errors := form.errors():
            self.set_status(errors[0], "error")
            return
        v = form.values()
        mirror_dir = Path(v["settings-mirror-dir"])
        moved = mirror_dir != self.app.cfg.settings.mirror_dir
        try:
            # The theme shown is saved too: after a fallback from a missing
            # theme, Save is how the stale name in mmw.toml gets replaced.
            await self.app.save_settings(
                mirror_dir=mirror_dir,
                max_connections=v["settings-max-connections"],
                disk_write_threads=v["settings-disk-write-threads"],
                disk_read_threads=v["settings-disk-read-threads"],
                disk_readahead=v["settings-disk-readahead"],
                update_check=v["settings-update-check"],
                theme=self.app.theme,
            )
        except Exception as e:  # noqa: BLE001 — reported on the page
            self.set_status(f"Not saved: {e}", "error")
            return
        self.query_one("#settings-database-path", Label).update(
            escape(str(self.app.cfg.settings.database)))
        self.set_status("Saved." + (f" Pool directory is now {mirror_dir} — nothing was moved."
                                    if moved else ""), "ok")

    @on(Button.Pressed, "#settings-signing-keys")
    def _signing_keys_pressed(self) -> None:
        from mmw.tui.signing_keys import SigningKeysPage
        self.app.push_screen(SigningKeysPage())

    @on(Button.Pressed, "#settings-database")
    def _move_pressed(self) -> None:
        self.start_flow(self._move_flow)

    async def _move_flow(self) -> None:
        from mmw.tui.path_pick import PathPickPage
        db = Path(self.app.cfg.settings.database)
        picked = await self.app.push_screen_wait(PathPickPage(
            "Move the database", "Choose the directory to keep the pool database in",
            start=str(db.parent), dirs_only=True))
        if picked is None:
            return
        dest = Path(picked) / db.name
        if not await self.app.push_screen_wait(ConfirmDialog(
                "Move the database",
                f"Copy the pool database to [b]{escape(str(dest))}[/b] and use it from "
                "there? The old file is removed once the copy is checked.", "Move")):
            return
        await self._move_to(dest)

    async def _move_to(self, dest: Path) -> None:
        from mmw.core.database_move import DatabaseMoveError, move_database

        def progress(done: int, total: int) -> None:
            pct = done * 100 // total if total else 100
            self.app.call_from_thread(self.set_status, f"Copying the database… {pct}%")
        self.set_status("Moving the database…")
        warnings: list[str] = []
        try:
            new = await asyncio.to_thread(lambda: move_database(
                self.app.cfg_dir, self.app.cfg, dest, progress=progress, warnings=warnings))
        except DatabaseMoveError as e:
            self.set_status(f"Database not moved: {e}", "error")
            return
        # mmw.toml already says so; adopt it through the one config writer.
        dest = new.settings.database
        await self.app.save_config(lambda c: c.model_copy(update={
            "settings": c.settings.model_copy(update={"database": dest})}))
        self.query_one("#settings-database-path", Label).update(escape(str(dest)))
        if warnings:
            self.set_status(f"Database moved to {dest}, but {warnings[0]}", "error")
        else:
            self.set_status(f"Database moved to {dest}.", "ok")

    @on(Button.Pressed, "#btn-settings-reinit")
    def _reinit(self) -> None:
        def answer(yes: bool | None) -> None:
            if yes:
                self.app.run_worker(self.app.reinitialise(), group="reinit",
                                    exit_on_error=False)
        self.app.push_screen(ConfirmDialog(
            "⚠  Re-initialize",
            "This deletes [b]mmw.toml[/b] and runs setup again. Downloaded packages are NOT "
            "deleted.", "Re-initialize"), answer)
