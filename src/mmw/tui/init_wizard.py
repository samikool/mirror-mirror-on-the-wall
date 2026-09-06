"""Setup wizard (InitWizardApp) and the shared add-repository flow.

The distro catalog is gone (see repository-first-redesign.md): a repository is
identified by its URL, so AddMirrorScreen is now the repository step followed
by confirm, with no version/arch/repo discovery chain in between.
"""
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Static

from mmw.core import config
from mmw.tui.custom_repo import StepCustom
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
        sources = await self.push_screen_wait(AddMirrorScreen())
        if not sources:
            self.exit()
            return
        self._planned_sources = sources

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

    @on(Button.Pressed, "#btn-next")
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
            self._write_config()
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

    def _write_config(self):
        cfg = config.Config(
            settings=config.Settings(
                mirror_dir=self._mirror_dir.resolve(),
                max_connections=self._max_connections,
            ),
            sources=self._planned_sources,
        )
        config.save(cfg, self._cfg_dir)


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

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._indicator_text(), id="step-indicator")
            with ScrollableContainer(id="step-container"):
                yield StepCustom(self._existing_urls)
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

    async def _mount_step(self, widget, next_label: str = "Next →") -> None:
        container = self.query_one("#step-container", ScrollableContainer)
        await container.remove_children()
        await container.mount(widget)
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
        try:
            current = container.query_one(WizardStep)
        except Exception:
            return  # loading/error placeholder — nothing to advance from

        if self._step_idx == self._REPO:
            sources = current.get_sources()  # type: ignore[attr-defined]
            if not sources:
                return
            self._sources = sources
            self._step_idx = self._CONFIRM
            await self._mount_step(StepConfirm(sources), next_label="Add ✓")
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
        await self._mount_step(StepCustom(self._existing_urls))
