"""`SiteApp` — mmw's TUI on the far side, for one site."""
from pathlib import Path

from mmw.site.home import load_site
from mmw.tui.ui.base import ThemedApp


class SiteApp(ThemedApp):
    def __init__(self, home: Path):
        super().__init__()
        self.home = Path(home)
        self.settings = load_site(self.home)
        self.title = f"mmw · {Path(self.settings.root).name} (far side)"
        self.sub_title = f"home {self.home}"

    def on_mount(self) -> None:
        from mmw.tui.site.home import SiteHomePage
        self.push_screen(SiteHomePage())
