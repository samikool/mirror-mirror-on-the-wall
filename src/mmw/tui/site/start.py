"""The far-side TUI's first screen when this machine has opened no mirror:
apply a bundle (the first apply makes its roots), or open a root."""
from textual import on, work
from textual.app import ComposeResult
from textual.widgets import Static

from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


class StartPage(Page):
    TITLE = "mmw · far side"
    HELP = ("Apply a bundle: pick the .tar (or stage folder), then where each hostname goes.\n"
            "Open a mirror: a folder mmw already serves from (its roots find each other).")

    def compose_body(self) -> ComposeResult:
        from mmw.site import recents
        yield Static("No mirror opened on this machine yet.", id="start-none")
        yield Button("Apply a bundle…", id="start-apply", variant="primary")
        yield Static("[dim]  a .tar or a stage folder[/]")
        yield Button("Open a mirror…", id="start-open")
        yield Static("[dim]  a folder mmw already serves from[/]")
        recent = recents.known()
        yield Static("Recent: " + (", ".join(recent) if recent else "none"), id="start-recent", markup=False)

    def status_text(self) -> str:
        return str(self.query_one("#page-status", Static).render())

    @on(Button.Pressed, "#start-apply")
    def _pick_bundle(self) -> None:
        from mmw.tui.path_pick import PathPickPage
        from mmw.tui.site.apply_flow import recent_paths
        recent = recent_paths()
        self.app.push_screen(PathPickPage("Apply a bundle", "a bundle .tar, or a stage directory",
                                          recent[0] if recent else "", dirs_only=False, suffixes=(".tar",)),
                             lambda path: path and self.apply_path(path))

    @on(Button.Pressed, "#start-open")
    def _pick_root(self) -> None:
        from mmw.tui.path_pick import PathPickPage
        self.app.push_screen(PathPickPage("Open a mirror", "a root: a folder mmw serves from"),
                             lambda path: path and self.open_path(path))

    def open_path(self, path: str) -> None:
        from mmw.site import recents
        from mmw.site.home import read_root
        from mmw.site.mset import open_set
        info = read_root(path)
        if info is None:
            self.set_status(f"no mirror at {path}", "error")
            return
        recents.remember(path)
        self.app.mset = open_set(set_id=info.set_id)
        self.app._titles()
        from mmw.tui.site.home import SiteHomePage
        self.app.switch_screen(SiteHomePage())

    @work(thread=True, exclusive=True, group="start-plan", exit_on_error=False)
    def apply_path(self, path: str) -> None:
        from mmw.site.apply import plan_set_apply
        self.app.call_from_thread(self.set_status, f"reading {path}…")
        try:
            plan = plan_set_apply(None, path)
        except Exception as e:  # noqa: BLE001 — not a bundle, and the like
            self.app.call_from_thread(self.set_status, str(e), "error")
            return
        self.app.call_from_thread(self._place, plan, path)

    def _place(self, plan, path: str) -> None:
        from mmw.tui.site.apply_flow import remember
        from mmw.tui.site.place import PlacePage
        remember(path)
        self.set_status("")
        self.app.push_screen(PlacePage(plan, path, None), self._placed)

    def _placed(self, choice) -> None:
        if choice is None:
            return
        from mmw.tui.site.apply_flow import ApplyRunPage
        self.app.push_screen(ApplyRunPage(choice), lambda _: self._opened())

    def _opened(self) -> None:
        """After a first apply: the mirror it made is this machine's now."""
        from mmw.site.mset import open_set
        try:
            self.app.mset = open_set()
        except (LookupError, ValueError):
            return                                   # it failed before making a root
        self.app._titles()
        from mmw.tui.site.home import SiteHomePage
        self.app.switch_screen(SiteHomePage())
