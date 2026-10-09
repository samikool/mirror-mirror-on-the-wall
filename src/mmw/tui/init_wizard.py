"""Setup wizard (InitWizardApp) and the shared add-repository flow.

The distro catalog is gone: a repository is
identified by its URL, so AddMirrorScreen is now the repository step followed
by confirm, with no version/arch/repo discovery chain in between.
"""
import asyncio
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Static

from mmw.core import config
from mmw.tui.custom_repo import StepCustom
from mmw.tui.modals import ConfirmModal, InfoModal
from mmw.tui.widgets import Button
from mmw.tui.wizard_steps import StepConfirm, StepSettings, WizardStep

_INIT_STEPS = ["Settings", "Confirm"]


class InitWizardApp(App):
    TITLE = "mirror-mirror-on-the-wall"
    SUB_TITLE = "setup wizard"

    DEFAULT_CSS = """
    Screen { layout: vertical; }

    #step-container {
        height: 1fr;
        border: solid $primary-darken-2;
        margin: 1 2;
    }

    #nav-bar {
        height: auto;   /* +1 row for border-top; 3-row buttons need 3 content rows */
        align: right middle;
        padding: 0 2;
        background: $surface;
        border-top: solid $primary-darken-2;
    }

    #nav-bar Button { margin-left: 1; }

    #step-indicator {
        height: 1;
        background: $primary-darken-3;
        padding: 0 2;
        color: $text-muted;
    }
    """

    BINDINGS = [Binding("q", "quit", "Quit")]

    def __init__(self, cfg_dir: Path, mirror_dir: Path | None = None, max_connections: int | None = None):
        super().__init__()
        self._cfg_dir = cfg_dir
        self._step_idx = 0  # 0 = settings, 1 = confirm
        self._planned_sources: list[config.SourceConfig] = []
        self._mirror_dir = mirror_dir if mirror_dir is not None else Path("./mirror")
        self._max_connections = max_connections if max_connections is not None else 10

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(self._indicator_text(), id="step-indicator")
        with ScrollableContainer(id="step-container"):
            yield StepSettings(str(self._mirror_dir), self._max_connections)
        with Horizontal(id="nav-bar"):
            yield Button("Back", id="btn-back", variant="default", disabled=True)
            yield Button("Next →", id="btn-next", variant="primary")
        yield Footer()

    def on_mount(self):
        self._pick_mirrors()

    @work
    async def _pick_mirrors(self):
        """Ask for repositories, and keep asking until told to stop.

        `AddMirrorScreen` dismisses `[]` on escape, and this used to read that
        as `self.exit()` — so a reflex escape ended the whole setup session
        with no config and no message, while the modal's own footer advertised
        "escape  Cancel", which implies it cancels the *screen*. Confirm first,
        and re-show the screen when the answer is no.
        """
        while True:
            sources = await self.push_screen_wait(AddMirrorScreen())
            if sources:
                self._planned_sources = sources
                return
            quit_setup = await self.push_screen_wait(ConfirmModal(
                "Quit setup?",
                "No repositories have been added yet, so nothing will be written.\n"
                "Leave setup, or go back and add one?",
                confirm_label="Quit setup",
                cancel_label="Go back",
            ))
            if quit_setup:
                self.exit()
                return

    def _indicator_text(self) -> str:
        parts = []
        for i, label in enumerate(_INIT_STEPS):
            if i == self._step_idx:
                parts.append(f"[bold white] {label} [/]")
            elif i < self._step_idx:
                parts.append(f"[dim] ✓ {label} [/]")
            else:
                parts.append(f"[dim]  {label} [/]")
        return "  ".join(parts)

    def _update_indicator(self):
        self.query_one("#step-indicator", Static).update(self._indicator_text())

    # `@work`, because `_write_config` may now have to push an `InfoModal` and
    # wait on it — `push_screen_wait` requires a worker context.
    @on(Button.Pressed, "#btn-next")
    @work(exit_on_error=False)
    async def next_step(self):
        container = self.query_one("#step-container", ScrollableContainer)
        current = container.query_one(WizardStep)

        if self._step_idx == 0:
            self._mirror_dir, self._max_connections = current.get_settings()  # type: ignore
            self._step_idx = 1
            self._update_indicator()
            container.remove_children()
            await container.mount(
                StepConfirm(self._planned_sources, self._mirror_dir, self._max_connections)
            )
            self.query_one("#btn-next", Button).label = "Write Config ✓"

        elif self._step_idx == 1:
            if not await self._write_config():
                return
            self.exit(result="done")

        self.query_one("#btn-back", Button).disabled = self._step_idx == 0

    @on(Button.Pressed, "#btn-back")
    async def prev_step(self):
        if self._step_idx == 0:
            return
        self._step_idx -= 1
        self._update_indicator()
        container = self.query_one("#step-container", ScrollableContainer)
        container.remove_children()
        self.query_one("#btn-next", Button).label = "Next →"
        # Restore previously entered values instead of resetting to defaults
        await container.mount(StepSettings(str(self._mirror_dir), self._max_connections))
        self.query_one("#btn-back", Button).disabled = True

    async def _write_config(self) -> bool:
        """Write `mmw.toml`. True on success; False leaves the wizard standing.

        `config.Config(...)` is the one place validation runs, and this is the
        one place the wizard constructs one — from a button handler, with no
        try/except, on the final press of a whole setup session. Two pasted
        repositories whose derived names collide (one host serving several, a
        `$(ARCH)` fan-out) raised a `ValidationError` straight through the
        handler: raw traceback, app dead, no config written, every pick lost.

        `merge_sources` is what makes the collision a non-event —
        `ManageApp.action_add_mirror` was only ever safe because it routes
        through it, and it renames rather than rejecting. The try/except is
        still needed: it is the last guard before the config is written, and a
        refusal has to be readable rather than a traceback.
        """
        try:
            sources, _, _ = config.merge_sources([], self._planned_sources)
            cfg = config.Config(
                settings=config.Settings(
                    mirror_dir=self._mirror_dir.resolve(),
                    max_connections=self._max_connections,
                ),
                sources=sources,
            )
            # Off the loop: `save()` validates the whole config and then
            # writes, and the wizard is the one place a user sits watching
            # the moment it happens.
            await asyncio.to_thread(config.save, cfg, self._cfg_dir)
        except Exception as e:
            await self.push_screen_wait(InfoModal(
                "Could not write mmw.toml",
                f"{e}\n\nNothing has been written. Go back and adjust the "
                "repositories, or quit and import the config with `mmw add -f`.",
            ))
            return False
        return True


# ── Add-mirror modal (shared with ManageApp) ──────────────────────────────────

class AddMirrorScreen(ModalScreen):
    """Repository → Confirm; dismisses with list[SourceConfig]."""

    DEFAULT_CSS = """
    AddMirrorScreen {
        align: center middle;
    }
    AddMirrorScreen > Vertical {
        width: 90%;
        height: 90%;
        border: double $primary;
        background: $surface;
    }
    AddMirrorScreen #step-indicator {
        height: 1;
        background: $primary-darken-3;
        padding: 0 2;
        color: $text-muted;
    }
    AddMirrorScreen #step-container {
        height: 1fr;
        border: solid $primary-darken-2;
        margin: 1 2 0 2;
    }
    AddMirrorScreen #nav-bar {
        height: auto;   /* +1 row for border-top; 3-row buttons need 3 content rows */
        align: right middle;
        padding: 0 2;
        background: $surface;
        border-top: solid $primary-darken-2;
    }
    AddMirrorScreen #nav-bar Button { margin-left: 1; }
    AddMirrorScreen .loading-msg {
        padding: 2;
        color: $text-muted;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    _REPO, _CONFIRM = 0, 1
    _STEPS = ["Repository", "Confirm"]

    def __init__(self, existing_units: set[tuple[str, str, str, str]] | None = None,
                 existing_urls: set[str] | None = None):
        super().__init__()
        self._existing_units = existing_units or set()
        self._existing_urls = existing_urls or set()
        self._step_idx = self._REPO
        self._sources: list[config.SourceConfig] = []
        # ONE repository step for the life of the screen. Back from Confirm
        # used to mount a brand-new `StepCustom`, which threw away the pasted
        # text, every probe result, every dimension pick and the whole
        # key-directory scan — and Confirm is precisely where a problem is
        # noticed, so Back is the button you press *because* something needs
        # changing. It is hidden rather than unmounted, so its state is simply
        # still there when it comes back.
        self._repo_step = StepCustom(self._existing_urls)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._indicator_text(), id="step-indicator")
            with ScrollableContainer(id="step-container"):
                yield self._repo_step
            with Horizontal(id="nav-bar"):
                yield Button("Cancel", id="btn-cancel", variant="default")
                yield Button("← Back", id="btn-back", variant="default", disabled=True)
                yield Button("Next →", id="btn-next", variant="primary")

    def on_mount(self):
        self._update_indicator()

    def _indicator_text(self) -> str:
        parts = []
        for i, label in enumerate(self._STEPS):
            if i == self._step_idx:
                parts.append(f"[bold $accent]{label}[/]")
            elif i < self._step_idx:
                parts.append(f"[dim]{label}[/]")
            else:
                parts.append(label)
        return "  " + "  →  ".join(parts)

    def _update_indicator(self):
        self.query_one("#step-indicator", Static).update(self._indicator_text())

    def _chrome(self, next_label: str) -> None:
        self.query_one("#btn-next", Button).label = next_label
        self.query_one("#btn-back", Button).disabled = self._step_idx == self._REPO
        self._update_indicator()

    def action_cancel(self):
        self.dismiss([])

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self):
        self.dismiss([])

    @on(Button.Pressed, "#btn-next")
    async def next_step(self):
        container = self.query_one("#step-container", ScrollableContainer)

        if self._step_idx == self._REPO:
            sources = self._repo_step.get_sources()
            if not sources:
                return
            self._sources = sources
            self._step_idx = self._CONFIRM
            # Hidden, never unmounted — see `__init__`. A fresh `StepCustom`
            # on the way back discarded everything the user had built.
            self._repo_step.display = False
            await container.mount(StepConfirm(sources))
            self._chrome("Add \u2713")
            return

        include_sources = container.query_one(StepConfirm).get_include_sources()
        for s in self._sources:
            if s.provider in ("apt", "apt-flat"):
                s.include_sources = include_sources
        self.dismiss(self._sources)

    @on(Button.Pressed, "#btn-back")
    async def prev_step(self):
        if self._step_idx == self._REPO:
            return
        self._step_idx = self._REPO
        self._sources = []
        container = self.query_one("#step-container", ScrollableContainer)
        for confirm in list(container.query(StepConfirm)):
            await confirm.remove()
        self._repo_step.display = True
        self._chrome("Next \u2192")
