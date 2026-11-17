"""Setup — first run (no mmw.toml) and re-initialise, inside MmwApp.

Settings → Sources → Confirm, then the config is written and the app
carries on into Sources. The sources step opens the add flow the first time;
cancelling it returns to Setup. Sources are optional: finish now and add
them later, with or without a recovered configuration.
"""
import asyncio
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.markup import escape
from textual.widgets import Input, Label, Static

from mmw.core import config
from mmw.tui.add_source import AddSourcePage
from mmw.tui.ui.dialog import ConfirmDialog
from mmw.tui.ui.wizard import Wizard
from mmw.tui.ui.widgets import Button
from mmw.tui.wizard_steps import StepConfirm, StepSettings


def build_config(mirror_dir: Path, max_connections: int,
                 planned: list[config.SourceConfig], recovered: config.Config | None = None) -> config.Config:
    """The config Setup writes. `merge_sources` makes two pasted repositories
    whose derived names collide a non-event (it renames rather than
    rejecting); `Config(...)` still validates, and the caller reports a
    refusal instead of a traceback. **Blocking** (`resolve()`)."""
    sources, _, _ = config.merge_sources([], planned)
    settings = (recovered.settings.with_mirror_dir(mirror_dir.resolve()) if recovered is not None
                else config.Settings(mirror_dir=mirror_dir.resolve()))
    settings = config.Settings(**{**settings.model_dump(), "max_connections": max_connections})
    return config.Config(settings=settings, sources=sources,
                         networks=recovered.networks if recovered is not None else [])


class SetupPage(Wizard):
    TITLE = "Set up mmw"
    STEPS = ("Settings", "Sources", "Confirm")
    FINISH_LABEL = "Write config ✓"

    def __init__(self):
        super().__init__()
        self._planned: list[config.SourceConfig] = []
        self._offered_add = False
        self._mirror_dir = Path("./mirror")
        self._max_connections = 10
        self._bundle_paths: list[Path] = []
        self._recovered: config.Config | None = None

    def compose_body(self) -> ComposeResult:
        yield from super().compose_body()
        defaults = getattr(self.app, "setup_defaults", {})
        yield StepSettings(str(defaults.get("mirror_dir", "./mirror")),
                           defaults.get("max_connections"))
        with Vertical(id="setup-bundle-start"):
            yield Button("Start from a bundle…", id="btn-setup-from-bundle", compact=True)
            yield Label("", id="setup-bundle-notes", markup=False)
        with Vertical(id="setup-repos"):
            yield Label("", id="setup-repo-count")
            yield Static("", id="setup-repo-list")
            yield Button("Add sources…", id="btn-setup-add", compact=True)
            yield Button("Review sources and networks…", id="btn-setup-review", compact=True)
        yield Label("", id="setup-recovered-networks", markup=False)
        yield Vertical(id="setup-confirm-slot")

    async def enter(self, step: int) -> None:
        self.query_one(StepSettings).display = step == 0
        self.query_one("#setup-bundle-start").display = step == 0
        self.query_one("#btn-setup-review").display = self._recovered is not None
        self.query_one("#setup-recovered-networks").display = step == 2 and self._recovered is not None
        self.query_one("#setup-recovered-networks", Label).update(
            "Recovered networks: " + ", ".join(n.name for n in self._recovered.networks)
            if self._recovered is not None else "")
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
                                         offer_source_packages=False))

    async def leave(self, step: int) -> bool:
        if step == 0:
            self._mirror_dir, self._max_connections = self.query_one(StepSettings).get_settings()
        self.set_status("")
        return True

    def _render_repos(self) -> None:
        n = len(self._planned)
        self.query_one("#setup-repo-count", Label).update(
            f"{n} source{'s' if n != 1 else ''} to mirror" if n else
            "No sources yet — you can add them later.")
        self.query_one("#setup-repo-list", Static).update("\n".join(
            f"[$primary]●[/] {escape(s.name)}  [dim]{escape(s.url)}[/]" for s in self._planned))

    @on(Button.Pressed, "#btn-setup-from-bundle")
    def _from_bundle_pressed(self) -> None:
        self.run_worker(self._from_bundle(), group="setup-recovery", exclusive=True)

    async def _from_bundle(self) -> None:
        from mmw.commands.init_from_bundle import prepare
        from mmw.tui.path_pick import PathPickPage
        picked = await self.app.push_screen_wait(PathPickPage(
            "Start from a bundle", "Choose a kept bundle; choose again to add another network.",
            dirs_only=False, suffixes=(".tar", ".jsonl", ".zst")))
        if picked is None:
            return
        paths = list(dict.fromkeys([*self._bundle_paths, Path(picked)]))
        mirror_dir, _ = self.query_one(StepSettings).get_settings()
        try:
            cfg, notes = await asyncio.to_thread(prepare, paths, mirror_dir)
        except (OSError, ValueError) as exc:
            self.set_status(str(exc), "error")
            return
        self._bundle_paths, self._recovered = paths, cfg
        self._planned = list(cfg.sources)
        self._offered_add = True
        self.query_one("#max-connections", Input).value = str(cfg.settings.max_connections)
        self.query_one("#setup-bundle-notes", Label).update(
            "Kept bundles: " + ", ".join(str(p) for p in paths) + "\n" + "\n".join(notes))
        self.set_status("Bundle loaded — review the Settings and Sources steps before writing.")

    @on(Button.Pressed, "#btn-setup-review")
    def _review_pressed(self) -> None:
        self.run_worker(self._review(), group="setup-review", exclusive=True)

    async def _review(self) -> None:
        from mmw.tui.config_recovery import BundleConfigReviewPage
        try:
            cfg = await asyncio.to_thread(build_config, self._mirror_dir, self._max_connections,
                                          self._planned, self._recovered)
        except ValueError as exc:
            self.set_status(str(exc), "error")
            return
        edited = await self.app.push_screen_wait(BundleConfigReviewPage(cfg))
        if edited is not None:
            self._recovered, self._planned = edited, list(edited.sources)
            self._mirror_dir, self._max_connections = edited.settings.mirror_dir, edited.settings.max_connections
            self.query_one("#mirror-dir", Input).value = str(self._mirror_dir)
            self.query_one("#max-connections", Input).value = str(self._max_connections)
            self._render_repos()

    @on(Button.Pressed, "#btn-setup-add")
    def _add_pressed(self) -> None:
        self.run_worker(self._add(), group="setup-add", exclusive=True)

    async def _add(self) -> None:
        sources = await self.app.push_screen_wait(AddSourcePage(
            existing_urls={s.identity for s in self._planned}))
        if sources:
            try:
                self._planned, _, _ = config.merge_sources(self._planned, sources)
            except ValueError as e:
                # One URI added as `apt` and again as `apt-flat`. Unhandled in
                # this worker it took the whole app down; it is a sentence.
                self.app.notify(str(e), severity="error", timeout=12)
                return
            self._render_repos()
            return

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
                                          self._planned, self._recovered)
            await asyncio.to_thread(config.save, cfg, self.app.cfg_dir)
        except Exception as e:  # noqa: BLE001 — a refusal must be readable
            message = (f"Could not write mmw.toml: {e} — nothing was written. Go back and adjust "
                       "the repositories, or quit and import with `mmw add -f`.")
            self.set_status(message, "error")
            self.notify(message, severity="error", timeout=20)
            return
        from mmw.core import disk_io
        disk_io.configure(cfg.settings)
        await self.app.finish_setup(cfg)
