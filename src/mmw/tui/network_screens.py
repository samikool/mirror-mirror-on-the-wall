"""Add/edit network wizard screens."""
import re
from dataclasses import dataclass
from pathlib import Path

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, Label, SelectionList, Static, Switch
from textual.widgets.selection_list import Selection

from mmw.core import config
from mmw.tui.path_pick import PathPickModal
from mmw.tui.widgets import Button, JumpSelectionList


class _StepNetworkPath(Widget):
    DEFAULT_CSS = """
    _StepNetworkPath { padding: 1 2; height: auto; }
    _StepNetworkPath .field-label { color: $text-muted; margin-bottom: 0; }
    _StepNetworkPath Input { margin-bottom: 1; }
    _StepNetworkPath #root-row { height: 3; }
    _StepNetworkPath #root-row Input { width: 1fr; margin-bottom: 0; }
    _StepNetworkPath #root-row Button { width: auto; min-width: 12; margin-left: 1; }
    _StepNetworkPath #preview {
        height: 3;
        color: $primary;
        border: solid $primary-darken-2;
        padding: 0 1;
        content-align: left middle;
        margin-top: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("Base path  (absolute path)", classes="field-label")
        with Horizontal(id="root-row"):
            yield Input(id="inp-root")
            yield Button("Browse…", id="btn-browse-root", variant="default")
        yield Label("Network name", classes="field-label")
        yield Input(id="inp-name")
        yield Label("", id="preview")

    def _update_preview(self) -> None:
        root = self.query_one("#inp-root", Input).value.strip()
        name = self.query_one("#inp-name", Input).value.strip()
        if root or name:
            self.query_one("#preview", Label).update(
                f"  [dim]→[/] [bold]{root or '…'}/{name or '…'}[/]"
            )
        else:
            self.query_one("#preview", Label).update("")

    @on(Input.Changed)
    def _any_changed(self, event: Input.Changed) -> None:
        self._update_preview()

    @on(Button.Pressed, "#btn-browse-root")
    def _browse_pressed(self, event: "Button.Pressed") -> None:
        event.stop()
        self._browse_root()

    @work
    async def _browse_root(self) -> None:
        """Pick the base path rather than typing it.

        Directories only, and the *base* is what gets picked — the network name
        below it is created by the publish, so it must not have to exist yet.
        """
        picked = await self.app.push_screen_wait(PathPickModal(
            "Where should this network publish?",
            "pick the base path — the network name is created inside it",
            self.query_one("#inp-root", Input).value.strip(),
        ))
        if picked is None:
            return                                  # cancelled: leave it alone
        self.query_one("#inp-root", Input).value = picked
        self._update_preview()

    def get_values(self) -> tuple[str, str]:
        return (
            self.query_one("#inp-root", Input).value.strip(),
            self.query_one("#inp-name", Input).value.strip(),
        )

    def restore(self, root: str, name: str) -> None:
        self.query_one("#inp-root", Input).value = root
        self.query_one("#inp-name", Input).value = name

class _StepNetworkMirrors(Widget):
    """Pick what the network publishes, by dimension rather than by name."""

    DEFAULT_CSS = """
    _StepNetworkMirrors { padding: 1 2; height: 1fr; overflow-y: auto; }
    _StepNetworkMirrors Label { margin-bottom: 0; }
    _StepNetworkMirrors .axis-title {
        color: $primary; text-style: bold; margin-top: 1;
        width: 1fr; height: 1; text-wrap: nowrap; text-overflow: ellipsis;
    }
    _StepNetworkMirrors .axis-dim  { color: $text-muted; }
    _StepNetworkMirrors .axis-list { height: auto; max-height: 6; }
    """

    def __init__(self, mirrors: list[config.MirrorConfig]):
        super().__init__()
        self._mirrors = mirrors
        self._axes = build_axes(mirrors, None)

    def compose(self) -> ComposeResult:
        yield Label("[bold]Choose what to publish[/]")
        yield Label("[dim]All selected  ·  ctrl+a toggles a list[/]")
        for axis in self._axes:
            yield from axis.compose_widgets()

    def get_selection(self) -> "list[config.NetworkSelect]":
        return [s for s in (a.to_select(self) for a in self._axes) if s is not None]

class _StepNetworkConfirm(Widget):
    DEFAULT_CSS = """
    _StepNetworkConfirm { padding: 1 2; }
    _StepNetworkConfirm Label { margin-bottom: 1; }
    _StepNetworkConfirm .mirror-entry { color: $primary; }
    _StepNetworkConfirm #du-row, _StepNetworkConfirm #certs-row {
        height: auto;   /* +1 row for border-top; 3-row buttons need 3 content rows */
        align: left middle;
        border-top: solid $primary-darken-2;
        margin-top: 1;
    }
    _StepNetworkConfirm #du-row Label, _StepNetworkConfirm #certs-row Label { margin: 0 0 0 1; }
    """

    def __init__(self, name: str, root: Path, selects: "list[config.NetworkSelect]"):
        super().__init__()
        self._name = name
        self._root = root
        self._selects = selects

    def compose(self) -> ComposeResult:
        yield Label("[bold]Confirm[/]")
        yield Label(f"  Network name:  [cyan]{self._name}[/]")
        yield Label(f"  Publish root:  [cyan]{self._root}[/]")
        yield Label(f"  Repositories:  [cyan]{len(self._selects)}[/]")
        yield Label("")
        for s in self._selects:
            dims = "  ".join(
                f"{name}: {', '.join(vals) if vals else 'all'}"
                for name, vals in (
                    ("suites", s.suites), ("components", s.components), ("arches", s.arches)
                )
            )
            yield Label(f"  [primary]●[/] {s.source}", classes="mirror-entry")
            yield Label(f"      [dim]{dims}[/]", classes="mirror-entry")
        with Horizontal(id="du-row"):
            yield Switch(value=False, id="du-switch")
            yield Label("Enable Ubuntu dist-upgrade support (serves changelogs.ubuntu.com)")
        with Horizontal(id="certs-row"):
            yield Switch(value=False, id="certs-switch")
            yield Label("Manage TLS certs for this network (generate key + CSR)")

    def get_dist_upgrade(self) -> bool:
        try:
            return self.query_one("#du-switch", Switch).value
        except Exception:
            return False

    def get_certs(self) -> bool:
        try:
            return self.query_one("#certs-switch", Switch).value
        except Exception:
            return False

@dataclass
class _Axis:
    """One repository's three dimension pickers inside a network editor."""
    source: str
    label: str
    suites: list[str]
    components: list[str]
    arches: list[str]
    chosen: "config.NetworkSelect | None"

    def _id(self, dim: str) -> str:
        return "axis-" + re.sub(r"[^a-zA-Z0-9_-]", "-", self.source) + "-" + dim

    def _picked(self, dim: str, available: list[str]) -> set[str]:
        """Empty in the stored select means all — show that as everything ticked."""
        if self.chosen is None:
            return set(available)
        stored = getattr(self.chosen, dim)
        return set(stored) if stored else set(available)

    def compose_widgets(self):
        yield Label(f"  [bold]{self.label}[/]  [dim]{self.source}[/]", classes="axis-title")
        for dim, values in (
            ("suites", self.suites),
            ("components", self.components),
            ("arches", self.arches),
        ):
            if not values:
                continue
            picked = self._picked(dim, values)
            yield Label(f"    [dim]{dim}[/]", classes="axis-dim")
            yield JumpSelectionList(
                *[Selection(v, v, v in picked) for v in values],
                id=self._id(dim),
                classes="axis-list",
            )

    def to_select(self, screen) -> "config.NetworkSelect | None":
        """None when nothing in this repository is ticked — it is left out."""
        picked = {}
        for dim, values in (
            ("suites", self.suites),
            ("components", self.components),
            ("arches", self.arches),
        ):
            if not values:
                continue
            try:
                sl = screen.query_one("#" + self._id(dim), SelectionList)
            except Exception:
                continue
            chosen = list(sl.selected)
            if not chosen:
                return None
            # Everything ticked is stored as "all", so the network keeps
            # following the repository as new suites or arches appear.
            picked[dim] = [] if set(chosen) == set(values) else chosen
        return config.NetworkSelect(source=self.source, **picked)


def build_axes(mirrors, existing_select) -> "list[_Axis]":
    """Group the pool's mirrors into one axis set per repository."""
    by_source: dict[str, list] = {}
    for m in mirrors:
        by_source.setdefault(m.source or m.mirror_base, []).append(m)
    chosen_by_source = {config.normalize_url(s.source): s for s in (existing_select or [])}

    axes = []
    for source, group in sorted(by_source.items()):
        axes.append(_Axis(
            source=source,
            label=group[0].name.split("-")[0] if group else source,
            suites=sorted({m.suite for m in group if m.suite}),
            components=sorted({m.component for m in group if m.component}),
            arches=sorted({m.arch for m in group if m.arch}),
            chosen=chosen_by_source.get(config.normalize_url(source)),
        ))
    return axes


class AddNetworkScreen(ModalScreen[config.NetworkConfig | None]):
    """3-step wizard: path & name → mirror selection → confirm."""

    DEFAULT_CSS = """
    AddNetworkScreen { align: center middle; }
    AddNetworkScreen > Vertical {
        width: 90%;
        height: 90%;
        border: double $primary;
        background: $surface;
    }
    AddNetworkScreen #step-indicator {
        height: 1;
        background: $primary-darken-3;
        padding: 0 2;
        color: $text-muted;
    }
    AddNetworkScreen #step-container {
        height: 1fr;
        border: solid $primary-darken-2;
        margin: 1 2 0 2;
    }
    AddNetworkScreen #nav-bar {
        height: auto;   /* +1 row for border-top; 3-row buttons need 3 content rows */
        align: right middle;
        padding: 0 2;
        background: $surface;
        border-top: solid $primary-darken-2;
    }
    AddNetworkScreen #nav-bar Button { margin-left: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, mirrors: list[config.MirrorConfig], existing: set[str] | None = None):
        super().__init__()
        self._mirrors = mirrors
        self._existing = existing or set()
        self._step_idx = 0
        self._root = ""
        self._name = ""
        self._selected_mirrors: "list[config.NetworkSelect]" = []

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._indicator_text(), id="step-indicator")
            with ScrollableContainer(id="step-container"):
                yield _StepNetworkPath()
            with Horizontal(id="nav-bar"):
                yield Button("Cancel", id="btn-cancel", variant="default")
                yield Button("Back", id="btn-back", variant="default", disabled=True)
                yield Button("Next →", id="btn-next", variant="primary")

    def on_mount(self) -> None:
        self._focus_step()

    def _indicator_text(self) -> str:
        labels = ["Path & Name", "Mirrors", "Confirm"]
        parts = []
        for i, label in enumerate(labels):
            if i == self._step_idx:
                parts.append(f"[bold white] {label} [/]")
            elif i < self._step_idx:
                parts.append(f"[dim] ✓ {label} [/]")
            else:
                parts.append(f"[dim]  {label} [/]")
        return "  ".join(parts)

    def _update_indicator(self) -> None:
        self.query_one("#step-indicator", Static).update(self._indicator_text())

    def _focus_step(self) -> None:
        try:
            inp = self.query_one("#step-container Input")
            inp.focus()
            return
        except Exception:
            pass
        try:
            self.query_one("#step-container SelectionList").focus()
        except Exception:
            pass

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-next")
    async def _next_step(self) -> None:
        container = self.query_one("#step-container", ScrollableContainer)

        if self._step_idx == 0:
            root, name = container.query_one(_StepNetworkPath).get_values()
            if not root or not name:
                self.notify("Both base path and name are required.", severity="warning")
                return
            if name in self._existing:
                self.notify(f"Network '{name}' already exists.", severity="warning")
                return
            self._root, self._name = root, name
            self._step_idx = 1
            self._update_indicator()
            container.remove_children()
            await container.mount(_StepNetworkMirrors(self._mirrors))

        elif self._step_idx == 1:
            selection = container.query_one(_StepNetworkMirrors).get_selection()
            if not selection:
                self.notify(
                    "Select at least one repository — nothing selected publishes nothing.",
                    severity="warning",
                )
                return
            self._selected_mirrors = selection
            self._step_idx = 2
            self._update_indicator()
            container.remove_children()
            await container.mount(
                _StepNetworkConfirm(self._name, Path(self._root) / self._name, self._selected_mirrors)
            )
            self.query_one("#btn-next", Button).label = "Add Network ✓"

        elif self._step_idx == 2:
            confirm = container.query_one(_StepNetworkConfirm)
            self.dismiss(
                config.NetworkConfig(
                    name=self._name,
                    root=Path(self._root) / self._name,
                    select=self._selected_mirrors,
                    dist_upgrade=confirm.get_dist_upgrade(),
                    certs=confirm.get_certs(),
                )
            )
            return

        self.query_one("#btn-back", Button).disabled = False
        self.call_after_refresh(self._focus_step)

    @on(Button.Pressed, "#btn-back")
    async def _prev_step(self) -> None:
        if self._step_idx == 0:
            return
        container = self.query_one("#step-container", ScrollableContainer)
        container.remove_children()
        self.query_one("#btn-next", Button).label = "Next →"

        if self._step_idx == 1:
            self._step_idx = 0
            step = _StepNetworkPath()
            await container.mount(step)
            self.call_after_refresh(lambda: step.restore(self._root, self._name))
        elif self._step_idx == 2:
            self._step_idx = 1
            await container.mount(_StepNetworkMirrors(self._mirrors))

        self._update_indicator()
        self.query_one("#btn-back", Button).disabled = self._step_idx == 0
        self.call_after_refresh(self._focus_step)

class EditNetworkScreen(ModalScreen[tuple[list[str], bool, bool, bool] | None]):
    """Edit the mirror list and settings for an existing network."""

    DEFAULT_CSS = """
    EditNetworkScreen { align: center middle; }
    EditNetworkScreen > Vertical {
        width: 90%;
        height: 90%;
        border: double $primary;
        background: $surface;
    }
    EditNetworkScreen #edit-title {
        height: 1;
        background: $primary-darken-3;
        padding: 0 2;
        color: $text-muted;
    }
    EditNetworkScreen #edit-container {
        height: 1fr;
        overflow-y: auto;   /* mirror list + 3 switch rows exceed a short screen */
        padding: 1 2;
    }
    EditNetworkScreen Label { margin-bottom: 1; }
    EditNetworkScreen SelectionList { height: 1fr; }
    EditNetworkScreen #prune-row, EditNetworkScreen #du-row, EditNetworkScreen #certs-row {
        height: auto;
        height: auto;   /* +1 row for border-top; 3-row buttons need 3 content rows */
        align: left middle;
        border-top: solid $primary-darken-2;
    }
    EditNetworkScreen #prune-row Label, EditNetworkScreen #du-row Label, EditNetworkScreen #certs-row Label {
        margin: 0 0 0 1;
        color: $text;
    }
    /* These hints ran off the right edge at 80/100 columns. `width: 1fr` alone
       fixed that but let them WRAP, and beside a switch in a narrow row the
       column got so thin the row grew to 60+ rows. Take the remaining width and
       truncate — a hint is expendable, the layout is not. */
    EditNetworkScreen #prune-row .prune-hint, EditNetworkScreen #du-row .du-hint, EditNetworkScreen #certs-row .certs-hint {
        color: $text-muted;
        margin: 0 0 0 2;
        width: 1fr;
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    /* Carries the full repository URI, which overruns 80 columns — truncate
       rather than wrap, as with the switch hints below. */
    EditNetworkScreen .axis-title {
        color: $primary;
        text-style: bold;
        margin-top: 1;
        width: 1fr;
        height: 1;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    EditNetworkScreen .axis-dim   { color: $text-muted; }
    EditNetworkScreen .axis-list  { height: auto; max-height: 8; margin-bottom: 0; }
    EditNetworkScreen #axis-scroll { height: 1fr; }
    EditNetworkScreen #nav-bar {
        height: auto;   /* +1 row for border-top; 3-row buttons need 3 content rows */
        align: right middle;
        padding: 0 2;
        background: $surface;
        border-top: solid $primary-darken-2;
        dock: bottom;
    }
    EditNetworkScreen #nav-bar Button { margin-left: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, network: config.NetworkConfig, all_mirrors: list[config.MirrorConfig]):
        super().__init__()
        self._network = network
        self._all_mirrors = all_mirrors
        self._axes = build_axes(all_mirrors, network.select)

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(f"  Editing  [bold]{self._network.name}[/]", id="edit-title")
            with Vertical(id="edit-container"):
                yield Label("[dim]What this network publishes  ·  ctrl+a toggles a list[/]")
                with ScrollableContainer(id="axis-scroll"):
                    for axis in self._axes:
                        yield from axis.compose_widgets()
                with Horizontal(id="prune-row"):
                    yield Switch(value=self._network.prune, id="prune-switch")
                    yield Label("Prune stale files on publish")
                    yield Label("(remove destination files no longer in the mirror source)", classes="prune-hint")
                with Horizontal(id="du-row"):
                    yield Switch(value=self._network.dist_upgrade, id="du-switch")
                    yield Label("Enable Ubuntu dist-upgrade support")
                    yield Label("(serves changelogs.ubuntu.com meta-release files + UPGRADE.md)", classes="du-hint")
                with Horizontal(id="certs-row"):
                    yield Switch(value=self._network.certs, id="certs-switch")
                    yield Label("Manage TLS certs for this network")
                    yield Label("(generate key + CSR covering all mirror hostnames)", classes="certs-hint")
            with Horizontal(id="nav-bar"):
                yield Button("Cancel", id="btn-cancel", variant="default")
                yield Button("Save", id="btn-save", variant="primary")

    def on_mount(self) -> None:
        try:
            self.query_one(SelectionList).focus()
        except Exception:
            pass

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-save")
    def _save(self) -> None:
        selects = [s for s in (a.to_select(self) for a in self._axes) if s is not None]
        if not selects:
            self.notify(
                "Select at least one repository — nothing selected publishes nothing.",
                severity="warning",
            )
            return
        prune = self.query_one("#prune-switch", Switch).value
        dist_upgrade = self.query_one("#du-switch", Switch).value
        certs = self.query_one("#certs-switch", Switch).value
        self.dismiss((selects, prune, dist_upgrade, certs))
