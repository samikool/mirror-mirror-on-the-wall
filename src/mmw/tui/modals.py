"""DeleteProgressModal — the progress modal a delete runs behind."""
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

from mmw.tui.ui.widgets import Button


class DeleteProgressModal(ModalScreen[None]):
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
