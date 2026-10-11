"""The section list down the left — or a one-row tab strip on narrow terminals
(the switch is pure CSS on `Page.-narrow`)."""
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import Static

from mmw.tui.navigation import SECTIONS


class SectionLink(Static):
    def __init__(self, mode: str, label: str, key: str):
        super().__init__(f"{key} {label}", id=f"nav-{mode}", classes="section-link",
                         markup=False)
        self.mode = mode

    async def on_click(self) -> None:
        await self.app.run_action(f"section('{self.mode}')")


class Sidebar(Vertical):
    def compose(self) -> ComposeResult:
        for s in SECTIONS:
            yield SectionLink(s.mode, s.label, s.key)

    def show_active(self, mode: str) -> None:
        for link in self.query(SectionLink):
            link.set_class(link.mode == mode, "-active")
