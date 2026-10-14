"""Wizard step widgets shared by SetupPage and AddSourcePage.

The distro catalog is gone, so the
version/arch/repo discovery steps that drove it went with it. What remains is
the provider-agnostic chrome: settings, and the confirm screen that renders
whatever sources the add flow produced.
"""
import os
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import ScrollableContainer, Vertical
from textual.markup import escape
from textual.widgets import Checkbox, Input, Label, Static

from mmw.core import config
from mmw.providers import get_provider
from mmw.tui.ui.form import Heading


class WizardStep(Static):
    """Base for each wizard step."""
    pass


def _unresolved(src: config.SourceConfig) -> bool:
    """True when this source's unit count cannot be known offline — the rule
    `RepositoryList.unresolved` applies to list rows, minus the pool half
    (nothing has been synced yet at Confirm time)."""
    return not get_provider(src.provider).expansion_known(src)


class StepSettings(WizardStep):

    def __init__(self, mirror_dir: str = "./mirror", max_connections: int | None = None):
        super().__init__()
        self._mirror_dir = mirror_dir
        self._max_connections = max_connections if max_connections is not None else (os.cpu_count() or 10)

    def compose(self) -> ComposeResult:
        # Not "Step 4": the catalog wizard that had four steps is gone, and
        # `_INIT_STEPS` renders this as step 1 of 2. The step indicator above
        # already says where we are.
        yield Heading("Settings")
        yield Label("Mirror storage directory", classes="field-label")
        yield Input(placeholder="/data/repos", id="mirror-dir", value=self._mirror_dir,
                    compact=True)
        yield Label("Max concurrent connections", classes="field-label")
        yield Input(placeholder="10", id="max-connections", value=str(self._max_connections),
                    compact=True)

    def get_settings(self) -> tuple[Path, int]:
        mirror_dir = Path(self.query_one("#mirror-dir", Input).value or "./mirror")
        try:
            conns = int(self.query_one("#max-connections", Input).value or "10")
        except ValueError:
            conns = 10
        return mirror_dir, conns

class StepConfirm(WizardStep):

    def __init__(
        self,
        sources: list[config.SourceConfig],
        mirror_dir: Path | None = None,
        max_connections: int | None = None,
        *,
        offer_source_packages: bool = True,
    ):
        """`offer_source_packages=False`: each add already decided deb-src for
        its own repositories (Setup), or the entry has no such option — one box
        here would overwrite them all."""
        super().__init__()
        self._sources = sources
        self._mirror_dir = mirror_dir
        self._max_connections = max_connections
        self._apt_sources = [s for s in sources if hasattr(s, "include_sources")]
        self._has_apt = offer_source_packages and bool(self._apt_sources)
        self._total_mirrors = sum(len(get_provider(s.provider).expand(s)) for s in sources)
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
        yield Heading("Confirm")
        if self._mirror_dir is not None:
            yield Label(f"Mirror directory: [$accent]{escape(str(self._mirror_dir))}[/]")
            yield Label(f"Connections:      [$accent]{self._max_connections}[/]")
        yield Label(
            f"Sources to add:   [$accent]{len(self._sources)}[/]  "
            f"([dim]{self._repo_count(self._total_mirrors, self._unresolved)}[/dim])"
        )
        with ScrollableContainer(id="confirm-list"):
            for s in self._sources:
                arches = get_provider(s.provider).source_summary(s)
                src = "  [dim]+ deb-src[/]" if getattr(s, "include_sources", False) else ""
                yield Label(
                    f"[$primary]●[/] {escape(s.name)}  [$accent]{escape(arches)}[/]{src}  "
                    f"[dim]{escape(s.url)}[/dim]",
                    classes="mirror-entry",
                )
        if self._has_apt:
            with Vertical(id="apt-options"):
                yield Heading("APT options")
                # Default from what arrived: a pasted config carrying
                # `deb-src` had this on, and forcing it off would silently
                # drop it on the way through Confirm.
                yield Checkbox("Include source packages (deb-src)",
                               value=any(s.include_sources for s in self._apt_sources),
                               id="src-switch", compact=True)
                yield Label(
                    "Downloads .dsc, .orig.tar.*, .debian.tar.* for every package. "
                    "Typically 3–10× larger than binaries alone.",
                    classes="opt-hint",
                )

    def get_include_sources(self) -> bool:
        if not self._has_apt:
            return False
        try:
            return self.query_one("#src-switch", Checkbox).value
        except Exception:
            return False
