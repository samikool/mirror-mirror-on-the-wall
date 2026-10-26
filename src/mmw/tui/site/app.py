"""`SiteApp` — mmw's TUI on the far side, for one mirror (a set of roots)."""
from pathlib import Path

from textual.binding import Binding

from mmw.tui.ui.base import ThemedApp
from mmw.tui.update_flow import UpdateOffers


class SiteApp(UpdateOffers, ThemedApp):
    BINDINGS = [Binding("q", "quit", "Quit")]
    def __init__(self, mset):
        super().__init__()
        self.mset = mset
        self._titles()

    def _titles(self) -> None:
        m = self.mset
        self.title = f"mmw · {m.network} (far side)" if m else "mmw (far side)"
        if m is None:
            self.sub_title = "no mirror opened yet"
        else:
            here = ", ".join(r.info.name for r in m.roots)
            gone = [f"{n} ({m.missing_reason.get(n, 'missing')})" for n in m.missing]
            self.sub_title = f"roots {here}" + (f" · missing {', '.join(gone)}" if gone else "")

    def reopen(self) -> None:
        """Read the set again: an apply or a move can add roots and hostnames."""
        from mmw.site.mset import open_set
        if self.mset is not None:
            self.mset = open_set(set_id=self.mset.set_id)
            self._titles()

    def root_of(self, repo: str):
        """The root holding `repo` (its interface: local now, ssh too)."""
        return self.mset.root_for_repo(repo)[0]

    def on_mount(self) -> None:
        import os
        from mmw.tui.update_flow import RESUME_BUNDLE
        self._start_update_offers(None)            # the far side never asks a network; the toast only
        resume = os.environ.pop(RESUME_BUNDLE, None)
        if resume:
            self.call_later(lambda: self.screen.resume_bundle(resume))

    def get_default_screen(self):
        """The first page is the app's own bottom screen: nothing is behind
        it, so Esc never leaves a blank screen with the app still running."""
        if self.mset is None:
            from mmw.tui.site.start import StartPage
            return StartPage()
        from mmw.tui.site.home import SiteHomePage
        return SiteHomePage()
