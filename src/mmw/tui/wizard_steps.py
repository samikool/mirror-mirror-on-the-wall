"""Wizard step widgets shared by InitWizardApp and AddMirrorScreen.

The distro catalog is gone (see docs/history/repository-first-redesign.md), so the
version/arch/repo discovery steps that drove it went with it. What remains is
the provider-agnostic chrome: settings, and the confirm screen that renders
whatever sources the add flow produced.
"""
import os
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.widgets import Input, Label, Static, Switch

from mmw.core import config


class WizardStep(Static):
    """Base for each wizard step."""
    pass


def _unresolved(src: config.SourceConfig) -> bool:
    """True when this source's mirror count cannot be known offline.

    The same rule `manage.ManageApp._unresolved` applies to the list rows,
    minus the pool half — nothing has been synced yet at Confirm time.
    """
    if not src.arches:
        return True
    return src.provider == "apt" and bool(src.suites) and not src.components


class StepSettings(WizardStep):
    DEFAULT_CSS = """
    StepSettings { padding: 1 2; }
    StepSettings Label { margin-bottom: 1; }
    StepSettings .field-label { color: $text-muted; margin-top: 1; }
    StepSettings Input { margin-bottom: 1; }
    """

    def __init__(self, mirror_dir: str = "./mirror", max_connections: int | None = None):
        super().__init__()
        self._mirror_dir = mirror_dir
        self._max_connections = max_connections if max_connections is not None else (os.cpu_count() or 10)

    def compose(self) -> ComposeResult:
        # Not "Step 4": the catalog wizard that had four steps is gone, and
        # `_INIT_STEPS` renders this as step 1 of 2. The step indicator above
        # already says where we are.
        yield Label("[bold]Settings[/]")
        yield Label("  [dim]Mirror storage directory[/]", classes="field-label")
        yield Input(placeholder="/data/repos", id="mirror-dir", value=self._mirror_dir)
        yield Label("  [dim]Max concurrent connections[/]", classes="field-label")
        yield Input(placeholder="10", id="max-connections", value=str(self._max_connections))

    def get_settings(self) -> tuple[Path, int]:
        mirror_dir = Path(self.query_one("#mirror-dir", Input).value or "./mirror")
        try:
            conns = int(self.query_one("#max-connections", Input).value or "10")
        except ValueError:
            conns = 10
        return mirror_dir, conns

class StepConfirm(WizardStep):
    DEFAULT_CSS = """
    StepConfirm { padding: 1 2; }
    StepConfirm Label { margin-bottom: 1; }
    StepConfirm .mirror-entry { color: $primary; }
    StepConfirm #apt-options {
        height: auto;
        border-top: solid $primary-darken-2;
        margin-top: 1;
        padding-top: 1;
    }
    StepConfirm #apt-options Label { margin: 0 0 0 1; }
    StepConfirm #apt-options .opt-hint { color: $text-muted; margin-left: 2; }
    StepConfirm #src-row { height: 3; align: left middle; }
    """

    def __init__(
        self,
        sources: list[config.SourceConfig],
        mirror_dir: Path | None = None,
        max_connections: int | None = None,
    ):
        super().__init__()
        self._sources = sources
        self._mirror_dir = mirror_dir
        self._max_connections = max_connections
        self._has_apt = any(s.provider in ("apt", "apt-flat") for s in sources)
        self._total_mirrors = sum(len(s.to_mirrors()) for s in sources)
        # `to_mirrors()` is pure and offline: with `components = []` or
        # `arches = []` — the documented "empty means all", and the ordinary
        # state of a freshly added repository — it expands to *nothing*. This
        # is the last screen before the config is written, and it told a
        # first-time user "(0 repos)" and printed a blank architecture, which
        # reads as "this did not work". Same honesty fix already applied to
        # `manage._unresolved()` and `MirrorDetail._dimension()`; it had never
        # reached here.
        self._unresolved = any(_unresolved(s) for s in sources)

    @staticmethod
    def _repo_count(total: int, unresolved: bool) -> str:
        if unresolved:
            return "all — resolved at sync"
        return f"{total} repo{'s' if total != 1 else ''}"

    def compose(self) -> ComposeResult:
        yield Label("[bold]Confirm[/]")
        if self._mirror_dir is not None:
            yield Label(f"  Mirror directory: [cyan]{self._mirror_dir}[/]")
            yield Label(f"  Connections:      [cyan]{self._max_connections}[/]")
        yield Label(
            f"  Sources to add:   [cyan]{len(self._sources)}[/]  "
            f"([dim]{self._repo_count(self._total_mirrors, self._unresolved)}[/dim])"
        )
        yield Label("")
        with ScrollableContainer():
            for s in self._sources:
                arches = ", ".join(s.arches) if s.arches else "all arches"
                yield Label(
                    f"  [primary]●[/] {s.name}  [cyan]{arches}[/]  [dim]{s.url}[/dim]",
                    classes="mirror-entry",
                )
        if self._has_apt:
            with Vertical(id="apt-options"):
                yield Label("[bold]APT options[/]")
                with Horizontal(id="src-row"):
                    # Default from what arrived: a pasted config carrying
                    # `deb-src` had this on, and forcing the switch to off would
                    # silently drop it on the way through Confirm.
                    yield Switch(
                        value=any(s.include_sources for s in self._sources),
                        id="src-switch",
                    )
                    yield Label("Include source packages (deb-src)")
                yield Label(
                    "Downloads .dsc, .orig.tar.*, .debian.tar.* for every package. "
                    "Typically 3–10× larger than binaries alone.",
                    classes="opt-hint",
                )

    def get_include_sources(self) -> bool:
        if not self._has_apt:
            return False
        try:
            return self.query_one("#src-switch", Switch).value
        except Exception:
            return False
