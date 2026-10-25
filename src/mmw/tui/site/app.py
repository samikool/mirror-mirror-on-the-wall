"""`SiteApp` — mmw's TUI on the far side, for one mirror (a set of roots)."""
from pathlib import Path

from mmw.tui.ui.base import ThemedApp


class SiteApp(ThemedApp):
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
            self.sub_title = f"roots {here}" + (f" · missing {', '.join(m.missing)}" if m.missing else "")

    def reopen(self) -> None:
        """Read the set again: an apply or a move can add roots and hostnames."""
        from mmw.site.mset import open_set
        if self.mset is not None:
            self.mset = open_set(set_id=self.mset.set_id)
            self._titles()

    def home_for(self, repo: str) -> Path:
        from mmw.site.ops import on_repo
        return on_repo(self.mset, repo)[0]

    def on_mount(self) -> None:
        if self.mset is None:
            from mmw.tui.site.start import StartPage
            self.push_screen(StartPage())
            return
        from mmw.tui.site.home import SiteHomePage
        self.push_screen(SiteHomePage())
