"""Setup — first run (no mmw.toml) and re-initialise, inside MmwApp.

Settings → Repositories → Confirm, then the config is written and the app
carries on into Repositories. The repository step opens the add flow for you
the first time; escaping it with nothing added asks whether to quit setup —
a reflex Esc used to end the whole session with no config and no message.
"""
import asyncio
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.markup import escape
from textual.widgets import Label, Static

from mmw.core import config
from mmw.tui.add_repository import AddRepositoryPage
from mmw.tui.ui.dialog import ConfirmDialog
from mmw.tui.ui.wizard import Wizard
from mmw.tui.ui.widgets import Button
from mmw.tui.wizard_steps import StepConfirm, StepSettings


def build_config(mirror_dir: Path, max_connections: int,
                 planned: list[config.SourceConfig]) -> config.Config:
    """The config Setup writes. `merge_sources` makes two pasted repositories
    whose derived names collide a non-event (it renames rather than
    rejecting); `Config(...)` still validates, and the caller reports a
    refusal instead of a traceback. **Blocking** (`resolve()`)."""
    sources, _, _ = config.merge_sources([], planned)
    return config.Config(
        settings=config.Settings(mirror_dir=mirror_dir.resolve(), max_connections=max_connections),
        sources=sources,
    )


class SetupPage(Wizard):
    TITLE = "Set up mmw"
    STEPS = ("Settings", "Repositories", "Confirm")
    FINISH_LABEL = "Write config ✓"

    def __init__(self):
        super().__init__()
        self._planned: list[config.SourceConfig] = []
        self._offered_add = False
        self._mirror_dir = Path("./mirror")
        self._max_connections = 10

    def compose_body(self) -> ComposeResult:
        yield from super().compose_body()
        defaults = getattr(self.app, "setup_defaults", {})
        yield StepSettings(str(defaults.get("mirror_dir", "./mirror")),
                           defaults.get("max_connections"))
        with Vertical(id="setup-repos"):
            yield Label("", id="setup-repo-count")
            yield Static("", id="setup-repo-list")
            yield Button("Add repositories…", id="btn-setup-add", compact=True)
        yield Vertical(id="setup-confirm-slot")

    async def enter(self, step: int) -> None:
        self.query_one(StepSettings).display = step == 0
        self.query_one("#setup-repos").display = step == 1
        slot = self.query_one("#setup-confirm-slot")
        await slot.remove_children()
        if step == 0:
            self.query_one("#mirror-dir").focus()
        elif step == 1:
            self._render_repos()
            self.query_one("#btn-setup-add").focus()
            if not self._planned and not self._offered_add:
                self._offered_add = True
                self.run_worker(self._add(), group="setup-add")
        else:
            await slot.mount(StepConfirm(self._planned, self._mirror_dir, self._max_connections,
                                         ask_sources=False))

    async def leave(self, step: int) -> bool:
        if step == 0:
            self._mirror_dir, self._max_connections = self.query_one(StepSettings).get_settings()
        elif step == 1 and not self._planned:
            self.set_status("Add at least one repository to continue.", "error")
            return False
        self.set_status("")
        return True

    def _render_repos(self) -> None:
        n = len(self._planned)
        self.query_one("#setup-repo-count", Label).update(
            f"{n} repositor{'y' if n == 1 else 'ies'} to mirror" if n else
            "No repositories yet.")
        self.query_one("#setup-repo-list", Static).update("\n".join(
            f"[$primary]●[/] {escape(s.name)}  [dim]{escape(s.url)}[/]" for s in self._planned))

    @on(Button.Pressed, "#btn-setup-add")
    def _add_pressed(self) -> None:
        self.run_worker(self._add(), group="setup-add", exclusive=True)

    async def _add(self) -> None:
        sources = await self.app.push_screen_wait(AddRepositoryPage(
            existing_urls={s.identity for s in self._planned}))
        if sources:
            self._planned, _, _ = config.merge_sources(self._planned, sources)
            self._render_repos()
            return
        if not self._planned:
            if await self.app.push_screen_wait(self._quit_dialog()):
                self.app.exit()

    def _quit_dialog(self) -> ConfirmDialog:
        return ConfirmDialog(
            "Quit setup?",
            "No configuration has been written yet. Leave setup, or go back and carry on?",
            confirm_label="Quit setup", cancel_label="Go back")

    def cancel(self) -> None:
        def answer(yes: bool | None) -> None:
            if yes:
                self.app.exit()
        self.app.push_screen(self._quit_dialog(), answer)

    async def finish(self) -> None:
        try:
            cfg = await asyncio.to_thread(build_config, self._mirror_dir, self._max_connections,
                                          self._planned)
            await asyncio.to_thread(config.save, cfg, self.app.cfg_dir)
        except Exception as e:  # noqa: BLE001 — a refusal must be readable
            message = (f"Could not write mmw.toml: {e} — nothing was written. Go back and adjust "
                       "the repositories, or quit and import with `mmw add -f`.")
            self.set_status(message, "error")
            self.notify(message, severity="error", timeout=20)
            return
        await self.app.finish_setup(cfg)
