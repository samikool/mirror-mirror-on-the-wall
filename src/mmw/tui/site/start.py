"""The far-side TUI's first screen when this machine has opened no mirror:
apply a bundle (the first apply makes its roots), or open a root."""
from textual import on, work
from textual.app import ComposeResult
from textual.widgets import Static

from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


class StartPage(Page):
    TITLE = "mmw · far side"
    HELP = ("Apply a bundle: pick the .tar (or stage folder), then choose where each hostname goes —\n"
            "an empty folder becomes a new root. Open a mirror: a folder that already holds one.")

    def __init__(self):
        super().__init__()
        self._suggested: list[str] = []          # empty folders offered as roots for the first apply

    def compose_body(self) -> ComposeResult:
        from mmw.site import recents
        yield Static("No mirror opened on this machine yet.", id="start-none")
        yield Button("Apply a bundle…", id="start-apply", variant="primary")
        yield Static("[dim]  first time: pick the bundle, then choose the folder(s) it goes in[/]")
        yield Button("Open a mirror…", id="start-open")
        yield Static("[dim]  a folder that already holds a mirror[/]")
        yield Static("", id="start-note")
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
            # Not a mirror yet — most likely where one should go: offered as
            # the root when a bundle is applied, never an error to puzzle over.
            if path not in self._suggested:
                self._suggested.append(path)
            self._note(f"{path} holds no mirror yet. To start one there: Apply a bundle… — this folder "
                       "is offered as the root for its hostnames.")
            return
        recents.remember(path)
        self.app.mset = open_set(set_id=info.set_id)
        self.app._titles()
        from mmw.tui.site.home import SiteHomePage
        self.app.switch_screen(SiteHomePage())

    def _note(self, text: str) -> None:
        self.query_one("#start-note", Static).update(text)

    def _busy(self, on: bool) -> None:
        for b in ("#start-apply", "#start-open"):
            self.query_one(b, Button).disabled = on

    def apply_path(self, path: str) -> None:
        from pathlib import Path
        self._busy(True)
        self._note(f"Reading {Path(path).name}… — its list of files and hostnames. A big bundle made before "
                   "sizes were recorded takes a minute or two (its headers are read); newer ones are instant.")
        self._plan_bundle(path)

    @work(thread=True, exclusive=True, group="start-plan", exit_on_error=False)
    def _plan_bundle(self, path: str) -> None:
        from mmw.site import apply as A
        try:
            plan = A.plan_set_apply(None, path)
        except Exception as e:  # noqa: BLE001 — not a bundle, and the like
            self.app.call_from_thread(self._failed, str(e))
            return
        self.app.call_from_thread(self._place, plan, path)

    def _failed(self, message: str) -> None:
        self._busy(False)
        self._note("")
        self.set_status(message, "error")

    def _place(self, plan, path: str) -> None:
        from mmw.tui.site.apply_flow import remember
        from mmw.tui.site.place import PlacePage
        remember(path)
        self.set_status("")
        self._busy(False)
        self._note("")
        page = PlacePage(plan, path, None, suggest=self._suggested)
        self.app.push_screen(page, self._placed)

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
