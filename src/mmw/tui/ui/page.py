"""`Page` — the one screen shape. Every section root and every sub-page is a
Page: sidebar · breadcrumb · scrolling body · one-row action bar · footer.

Sections are Textual modes, each with its own stack of Pages, so the breadcrumb
is just the TITLEs of the current mode's stack and Esc is just `pop_screen` —
except at a section's root, where there is nothing to go back to.
"""
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual import events
from textual.events import Resize
from textual.screen import Screen
from textual.widgets import Footer, Static

from mmw.tui.ui.hit_test import LiveHitTest
from mmw.tui.ui.sidebar import Sidebar
from mmw.tui.ui.widgets import Button

# Below this the sidebar becomes a one-row tab strip: at 80 columns an
# 18-column sidebar leaves forms too little room for label + control.
NARROW_WIDTH = 100

_STATUS_KINDS = ("info", "ok", "error")


class Page(LiveHitTest, Screen):
    TITLE = ""
    HELP = ""               # notes `?` shows under the page's keys (markup)
    SCROLLS = True          # False: the body is one 1fr list/tree, which must not scroll
    # Actions the bar has no button for: listed under More… (with their keys),
    # so nothing is reachable only by a key nobody can see.
    MORE: tuple[str, ...] = ()
    subtitle = ""           # e.g. the URL a page is about; shown under the breadcrumb
    BINDINGS = [Binding("escape", "back", "Back")]

    def compose(self) -> ComposeResult:
        with Horizontal(id="page-frame"):
            # No sidebar during Setup (nothing to navigate to yet), nor in a
            # bare ThemedApp hosting a page.
            if getattr(self.app, "HAS_SECTIONS", False):
                yield Sidebar()
            with Vertical(id="page-main"):
                yield Static("", id="breadcrumb", markup=False)
                if self.subtitle:
                    yield Static(self.subtitle, id="page-subtitle", markup=False)
                body = VerticalScroll if self.SCROLLS else Vertical
                with body(id="page-body"):
                    yield from self.compose_body()
                yield Static("", id="page-hint", markup=False)
                with Horizontal(id="page-actions"):
                    yield Static("", id="page-status", markup=False)
                    yield from self.compose_actions()
                    if self.MORE:
                        yield Button("More…", id="btn-page-more", compact=True)
        # The page's verbs and `? Help` only: help lists every key (q, ^p…),
        # and a key pushed off the right edge at 80 columns helps nobody.
        yield Footer(show_command_palette=False)

    def more_entries(self) -> list[tuple[str, str, str, bool]]:
        """(action, label, key, available) for each of MORE, from the page's
        own bindings — so the menu can never name a key that does something else."""
        out = []
        for action in self.MORE:
            binding = next((b for b in self._bindings_for(action)), None)
            label = binding.description if binding is not None else action.replace("_", " ").capitalize()
            key = self.app.get_key_display(binding) if binding is not None else ""
            out.append((action, label, key, self.check_action(action, ()) is not False))
        return out

    def _bindings_for(self, action: str):
        for _key, active in self._bindings.key_to_bindings.items():
            for b in active:
                if b.action == action:
                    yield b

    @on(Button.Pressed, "#btn-page-more")
    def _more_pressed(self, event) -> None:
        event.stop()
        self.action_more()

    def action_more(self) -> None:
        from mmw.tui.ui.dialog import MoreMenu

        def picked(action: "str | None") -> None:
            if action:
                self.run_worker(self.run_action(action), exit_on_error=False)
        self.app.push_screen(MoreMenu(self.TITLE or "This page", self.more_entries()), picked)

    def start_flow(self, flow) -> None:
        """Run `flow()` (an async action that may push pages) in a worker —
        one at a time, never by cancelling the one in flight: a cancelled
        remove could land on disk but not in memory, and the next write would
        quietly bring the thing back."""
        if getattr(self, "_flow_busy", False):
            self.notify("Still working on the last action — try again in a moment.",
                        severity="warning")
            return
        self._flow_busy = True

        async def run() -> None:
            try:
                await flow()
            except Exception as e:  # noqa: BLE001 — a failed action must never end the app
                self.notify(f"{type(e).__name__}: {e}" if str(e) else type(e).__name__,
                            severity="error", timeout=15)
            finally:
                self._flow_busy = False
        self.run_worker(run(), group="page-flow", exit_on_error=False)

    async def _on_message(self, message) -> None:
        # Quitting right after a page is pushed: its message loop had not
        # started, so shutdown closes its widgets one by one while its Mount
        # and ScreenResume are still queued — and every handler that queries
        # them would raise NoMatches and crash the exit. Textual has no public
        # hook for messages already queued.
        if not self.app.is_running and isinstance(message, (events.Mount, events.ScreenResume)):
            return
        await super()._on_message(message)

    def compose_body(self) -> ComposeResult:
        yield from ()

    def compose_actions(self) -> ComposeResult:
        yield from ()

    def breadcrumb(self) -> str:
        return " › ".join(s.TITLE for s in self.app.screen_stack
                          if isinstance(s, Page) and s.TITLE)

    def set_status(self, message: str, kind: str = "info") -> None:
        # A worker can finish while its page is being taken down (quitting,
        # or leaving right after opening it): the status line is already
        # gone, and there is no one to tell — saying so must not crash.
        status = next(iter(self.query("#page-status").results(Static)), None)
        if status is None:
            return
        status.update(message)
        for k in _STATUS_KINDS:
            status.set_class(k == kind, f"-{k}")

    def on_screen_resume(self) -> None:
        self.query_one("#breadcrumb", Static).update(self.breadcrumb())
        for sidebar in self.query(Sidebar):
            sidebar.show_active(self.app.current_mode)
        self._fit(self.app.size.width)
        self.refresh_bindings()

    def on_resize(self, event: Resize) -> None:
        self._fit(event.size.width)

    def _fit(self, width: int) -> None:
        self.set_class(width < NARROW_WIDTH, "-narrow")

    def on_descendant_focus(self, event) -> None:
        from mmw.tui.ui.form import Field
        field = next((a for a in event.widget.ancestors_with_self if isinstance(a, Field)), None)
        self.query_one("#page-hint", Static).update(field.hint if field else "")

    def _is_root(self) -> bool:
        stack = self.app.screen_stack
        return len(stack) <= 1 or stack[0] is self

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "back":
            return not self._is_root()
        return True

    def action_back(self) -> None:
        # dismiss, not pop: a page awaited with push_screen_wait must hand its
        # caller None on Esc, exactly as the modal it replaced did.
        if not self._is_root():
            self.dismiss(None)
