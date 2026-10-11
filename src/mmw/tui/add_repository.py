"""Add repositories: Repository → Confirm.

Dismisses with `list[SourceConfig]`, or None on Cancel/Esc. Used by the
Repositories section, the old `mmw manage`, and `mmw init`.
"""
from textual.app import ComposeResult
from textual.containers import Vertical

from mmw.core import config
from mmw.tui.custom_repo import StepCustom
from mmw.tui.ui.wizard import Wizard
from mmw.tui.wizard_steps import StepConfirm


class AddRepositoryPage(Wizard):
    TITLE = "Add repository"
    STEPS = ("Repository", "Confirm")
    FINISH_LABEL = "Add ✓"

    def __init__(self, existing_units: "set[tuple[str, str, str, str]] | None" = None,
                 existing_urls: "set[str] | None" = None):
        super().__init__()
        self._existing_units = existing_units or set()
        self._sources: list[config.SourceConfig] = []
        # ONE repository step for the page's life. Back from Confirm used to
        # mount a brand-new StepCustom, which threw away the pasted text,
        # every probe result, every dimension pick and the whole key-directory
        # scan — and Confirm is precisely where a problem is noticed, so Back
        # is the button pressed *because* something needs changing. Hidden on
        # Confirm, never unmounted.
        self._repo_step = StepCustom(existing_urls or set())

    def compose_body(self) -> ComposeResult:
        yield from super().compose_body()
        yield self._repo_step
        yield Vertical(id="confirm-slot")

    async def enter(self, step: int) -> None:
        self._repo_step.display = step == 0
        slot = self.query_one("#confirm-slot")
        await slot.remove_children()
        if step == 1:
            await slot.mount(StepConfirm(self._sources))
        else:
            self._sources = []
            # Focus the visible input: in paste mode the URL row is hidden, and
            # focus there let typing/Enter silently re-probe and drop the paste.
            row_shown = self.query_one("#url-row").display
            self.query_one("#custom-url" if row_shown else "#paste-area").focus()

    async def leave(self, step: int) -> bool:
        if step == 0:
            sources = self._repo_step.get_sources()
            if not sources:
                return False
            self._sources = sources
        return True

    def result(self) -> list[config.SourceConfig]:
        include = self.query_one(StepConfirm).get_include_sources()
        for s in self._sources:
            if s.provider in ("apt", "apt-flat"):
                s.include_sources = include
        return self._sources
