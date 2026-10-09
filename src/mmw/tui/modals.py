"""Generic modal dialogs shared across the manage TUI."""
import asyncio
import os
import threading
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, ProgressBar

from mmw.tui.widgets import Button


class InfoModal(ModalScreen):
    DEFAULT_CSS = """
    InfoModal { align: center middle; }
    InfoModal > Vertical {
        width: 70;
        max-width: 90%;
        height: auto;
        border: double $warning;
        background: $surface;
        padding: 1 2;
    }
    InfoModal Label { width: 100%; margin-bottom: 1; }
    InfoModal .modal-title { text-style: bold; color: $warning; }
    InfoModal Horizontal { align: center middle; height: auto; margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "dismiss_modal", "Close", show=False)]

    def __init__(self, title: str, message: str):
        super().__init__()
        self._title = title
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._title, classes="modal-title")
            yield Label(self._message)
            with Horizontal():
                yield Button("OK", variant="primary", id="ok")

    @on(Button.Pressed, "#ok")
    def dismiss_modal(self):
        self.dismiss()

    def action_dismiss_modal(self) -> None:
        self.dismiss()

class HelpModal(ModalScreen[None]):
    """What the keys do.

    There was no help anywhere in the app: multi-select, the `/` filter, the
    difference between prune and wipe and the fact that editing is the only
    route that can *narrow* a repository were all things you had to be told.
    The footer shows a handful of bindings and hides the rest.
    """

    DEFAULT_CSS = """
    HelpModal { align: center middle; }
    HelpModal > Vertical {
        width: 82;
        max-width: 96%;
        height: 80%;
        max-height: 92%;
        border: double $primary;
        background: $surface;
    }
    HelpModal #help-title {
        height: 1;
        background: $primary-darken-3;
        padding: 0 2;
        color: $text;
        text-style: bold;
    }
    HelpModal #help-body { height: 1fr; padding: 1 2; }
    HelpModal #help-bar {
        height: auto;
        align: right middle;
        padding: 0 2;
        background: $surface;
        border-top: solid $primary-darken-2;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_modal", "Close"),
        Binding("question_mark", "dismiss_modal", "Close", show=False),
    ]

    def __init__(self, title: str, body: str):
        super().__init__()
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        from textual.containers import VerticalScroll

        with Vertical():
            yield Label(f"  {self._title} — keys", id="help-title")
            with VerticalScroll(id="help-body"):
                yield Label(self._body)
            with Horizontal(id="help-bar"):
                yield Button("Close", id="btn-help-close", variant="primary")

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-help-close")
    def _close_help(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    ConfirmModal > Vertical {
        width: 60;
        max-width: 90%;
        height: auto;
        border: double $error;
        background: $surface;
        padding: 1 2;
    }
    ConfirmModal Label {
        width: 100%;
        text-align: center;
        margin-bottom: 1;
    }
    ConfirmModal .warn {
        color: $error;
        text-style: bold;
    }
    ConfirmModal Horizontal {
        align: center middle;
        height: auto;
        margin-top: 1;
    }
    ConfirmModal Button {
        margin: 0 2;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, title: str, message: str,
                 confirm_label: str = "Confirm", cancel_label: str = "Cancel",
                 confirm_variant: str = "error", cancel_variant: str = "primary"):
        super().__init__()
        self._title = title
        self._message = message
        self._confirm_label = confirm_label
        self._cancel_label = cancel_label
        self._confirm_variant = confirm_variant
        self._cancel_variant = cancel_variant

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(self._title, classes="warn")
            yield Label(self._message)
            with Horizontal():
                yield Button(self._cancel_label, variant=self._cancel_variant, id="cancel")
                yield Button(self._confirm_label, variant=self._confirm_variant, id="confirm")

    @on(Button.Pressed, "#cancel")
    def cancel(self):
        self.dismiss(False)

    def action_cancel(self) -> None:
        """Escape declines — it must never trigger the destructive branch."""
        self.dismiss(False)

    @on(Button.Pressed, "#confirm")
    def confirm(self):
        self.dismiss(True)

class DeleteProgressModal(ModalScreen[None]):
    DEFAULT_CSS = """
    DeleteProgressModal { align: center middle; }
    DeleteProgressModal > Vertical {
        width: 72;
        max-width: 90%;
        height: auto;
        border: double $primary;
        background: $surface;
        padding: 1 2;
    }
    DeleteProgressModal .modal-title { text-style: bold; color: $primary; margin-bottom: 1; }
    DeleteProgressModal ProgressBar  { margin: 1 0; }
    DeleteProgressModal #count       { color: $text-muted; }
    DeleteProgressModal Horizontal   { align: center middle; height: auto; margin-top: 1; }
    """

    BINDINGS = [Binding("escape", "noop", "", show=False)]

    def __init__(self, title: str, dirs: list[Path]):
        super().__init__()
        self._title = title
        self._dirs = [d for d in dirs if d.exists()]
        self._deleted = 0
        self._total = 0
        self._done = False
        self._lock = threading.Lock()

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(f"  {self._title}", classes="modal-title")
            yield Label("  Counting files…", id="status")
            yield ProgressBar(total=100, show_eta=False, id="bar")
            yield Label("", id="count")
            with Horizontal():
                yield Button("Close", id="btn-close", variant="primary", disabled=True)

    def on_mount(self) -> None:
        self.set_interval(1 / 10, self._tick)
        # run_worker, not ensure_future: Textual then owns the task, cancels it
        # if the modal goes away, and surfaces exceptions instead of dropping
        # them on an unreferenced future.
        self.run_worker(self._run(), name="delete-files", exit_on_error=False)

    def action_noop(self) -> None:
        pass  # block escape until done

    async def _run(self) -> None:
        total = await asyncio.to_thread(self._count_files)
        with self._lock:
            self._total = total
        await asyncio.to_thread(self._delete_files)
        with self._lock:
            self._done = True

    def _count_files(self) -> int:
        return sum(
            sum(len(files) for _, _, files in os.walk(d))
            for d in self._dirs
        )

    def _delete_files(self) -> None:
        for d in self._dirs:
            for dirpath, _, filenames in os.walk(d, topdown=False):
                for f in filenames:
                    try:
                        Path(dirpath, f).unlink()
                    except OSError:
                        pass
                    with self._lock:
                        self._deleted += 1
                try:
                    Path(dirpath).rmdir()
                except OSError:
                    pass

    def _tick(self) -> None:
        with self._lock:
            deleted = self._deleted
            total = self._total
            done = self._done

        if total > 0:
            self.query_one(ProgressBar).update(progress=deleted, total=total)
            self.query_one("#count", Label).update(f"  {deleted:,} / {total:,} files deleted")
        elif done:
            self.query_one("#status", Label).update("  Nothing to delete.")

        if done:
            if total > 0:
                self.query_one("#status", Label).update("  Done.")
            self.query_one("#btn-close", Button).disabled = False

    @on(Button.Pressed, "#btn-close")
    def _close(self) -> None:
        self.dismiss()
