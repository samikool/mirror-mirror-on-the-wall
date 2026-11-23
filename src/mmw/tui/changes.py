"""Changes: what changed between two points, as `mmw history diff` and
`mmw site diff` say it. Shared by both sides' history tables."""
import asyncio

from textual.app import ComposeResult
from textual.widgets import Static

from mmw.tui.ui.page import Page

# A whole release upgrade can change tens of thousands of packages: the
# page shows this many, and the CLI the rest.
SHOWN = 2000


class ChangesPage(Page):
    """`load()` (blocking: it reads both points' indexes) gives the
    `point_diff.SuiteDiff`s to show."""
    TITLE = 'Changes'
    HELP = ('The packages added (+), upgraded (~), downgraded (↓) and removed (−) from the older '
            'point to the newer one, read from both points\' Packages indexes '
            '(mmw history diff, mmw site diff).')

    def __init__(self, load, subtitle: str = ''):
        super().__init__()
        self._load_diffs = load
        self.subtitle = subtitle

    def compose_body(self) -> ComposeResult:
        yield Static('Reading both points\' indexes…', id='changes-text', markup=False)

    def on_mount(self) -> None:
        self.start_flow(self._load)

    async def _load(self) -> None:
        from mmw.core.point_diff import describe
        try:
            text = describe(await asyncio.to_thread(self._load_diffs), limit=SHOWN)
        except (ValueError, LookupError, OSError) as exc:
            text = str(exc)
        if self.is_mounted:
            self.query_one('#changes-text', Static).update(text)
