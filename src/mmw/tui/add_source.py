"""Add a source: What → <the entry's step> → Confirm.

"What are you adding?" lists one entry per provider with an Add step, then
"Detect from URL or paste…". `entry=` fixes the entry and skips the picker.
Dismisses with `list[SourceConfig]`, or None on Cancel/Esc. Used by Sources
and by Setup (which is also what `mmw init` opens).
"""
from textual import on
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from mmw.core import config
from mmw.tui.provider_ui import AddEntry, add_entries
from mmw.tui.ui.form import Heading
from mmw.tui.ui.wizard import Wizard
from mmw.tui.wizard_steps import StepConfirm


class AddSourcePage(Wizard):
    TITLE = "Add source"
    FINISH_LABEL = "Add ✓"

    def __init__(self, existing_units: "set[tuple[str, str, str, str]] | None" = None,
                 existing_urls: "set[str] | None" = None, entry: str | None = None):
        super().__init__()
        self._existing_units = existing_units or set()
        self._existing_urls = existing_urls or set()
        self.entries: list[AddEntry] = add_entries()
        self._fixed = entry is not None
        key = entry or self.entries[0].key
        self._entry = next((e for e in self.entries if e.key == key), None)
        if self._entry is None:
            raise ValueError(f"no Add entry {key!r} — known: "
                             + ", ".join(e.key for e in self.entries))
        self._sources: list[config.AnySource] = []
        self._confirmation = None
        # One step widget per entry for the page's life: Back must find a
        # step exactly as it was left (pasted text, probe results, picks).
        self._steps: dict[str, object] = {}
        self.STEPS = self._step_names()

    def _step_names(self) -> tuple[str, ...]:
        head = () if self._fixed else ("What",)
        return (*head, self._entry.step_title, "Confirm")

    @property
    def _offset(self) -> int:
        return 0 if self._fixed else 1

    def compose_body(self) -> ComposeResult:
        yield from super().compose_body()
        if not self._fixed:
            with Vertical(id="add-pick"):
                yield Heading("What are you adding?")
                yield OptionList(
                    *[Option(f"[b]{e.label}[/]\n[dim]{e.description}[/]", id=e.key)
                      for e in self.entries],
                    id="add-entry-list", compact=True)
        yield Vertical(id="add-step-slot")
        yield Vertical(id="confirm-slot")

    async def enter(self, step: int) -> None:
        if not self._fixed:
            self.query_one("#add-pick").display = step == 0
        slot = self.query_one("#add-step-slot")
        on_entry_step = step == self._offset
        if on_entry_step and self._entry.key not in self._steps:
            widget = self._entry.make_step(self._existing_urls)
            self._steps[self._entry.key] = widget
            await slot.mount(widget)
        for key, widget in self._steps.items():
            widget.display = on_entry_step and key == self._entry.key
        confirm = self.query_one("#confirm-slot")
        if self._confirmation is not None and hasattr(self._confirmation, "invalidate_checks"):
            self._confirmation.invalidate_checks()
        await confirm.remove_children()
        if step == 0 and not self._fixed:
            lst = self.query_one("#add-entry-list", OptionList)
            lst.highlighted = self.entries.index(self._entry)
            lst.focus()
        elif on_entry_step:
            self._sources = []
            self._steps[self._entry.key].focus_first()
        else:
            self._confirmation = (self._entry.make_confirm(self._sources) if self._entry.make_confirm
                                  else StepConfirm(self._sources,
                                                   offer_source_packages=self._entry.offer_source_packages))
            await confirm.mount(self._confirmation)

    async def leave(self, step: int) -> bool:
        if step == len(self.STEPS) - 1 and hasattr(self._confirmation, "can_finish"):
            if not self._confirmation.can_finish():
                self.set_status("Check the selection or explicitly save without checking after a failure.", "error")
                return False
        if step == 0 and not self._fixed:
            index = self.query_one("#add-entry-list", OptionList).highlighted
            self._entry = self.entries[index if index is not None else 0]
            self.STEPS = self._step_names()
            return True
        if step == self._offset:
            sources = self._steps[self._entry.key].get_sources()
            if not sources:
                return False
            self._sources = sources
        return True

    @on(OptionList.OptionSelected, "#add-entry-list")
    async def _entry_chosen(self, event: OptionList.OptionSelected) -> None:
        """Enter on an entry is Next."""
        event.stop()
        self.query_one("#btn-wizard-next").press()

    def result(self) -> list[config.AnySource]:
        if self._entry.offer_source_packages:
            include = self._confirmation.get_include_sources()
            for s in self._sources:
                if hasattr(s, "include_sources"):
                    s.include_sources = include
        return self._sources
