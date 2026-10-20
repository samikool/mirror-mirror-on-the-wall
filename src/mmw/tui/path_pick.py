"""Browsing for a path instead of typing it.

Every path this TUI asks for is somewhere the user has to go and look — a
directory of keyrings copied off another machine, an `/etc/apt/sources.list.d`
to import. Typing those blind is both tedious and easy to get wrong, and a typo
surfaces only as "no key files found".

Textual ships `DirectoryTree`, so this is a thin modal around it: browse, or
type into the same box if you already know the path. The two stay in sync —
highlighting a node fills the box, and submitting the box re-roots the tree.
"""
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.markup import escape
from textual.widgets import DirectoryTree, Input, Label

from mmw.core import disk_io
from mmw.tui.ui.page import Page
from mmw.tui.ui.widgets import Button


class _FilteredTree(DirectoryTree):
    """A tree showing directories, plus files of interest when asked."""

    def __init__(self, path, *, dirs_only: bool, suffixes: "tuple[str, ...]", **kwargs):
        self._dirs_only = dirs_only
        self._suffixes = suffixes
        super().__init__(path, **kwargs)

    def filter_paths(self, paths):
        out = []
        for p in paths:
            if p.name.startswith("."):
                continue        # dotfiles are noise here, never the target
            try:
                if disk_io.ops.is_dir(p):
                    out.append(p)
                elif not self._dirs_only and (
                    not self._suffixes or p.suffix.lower() in self._suffixes
                ):
                    out.append(p)
            except OSError:
                continue        # broken symlink or unreadable — just skip it
        return out


class PathPickPage(Page):
    """Pick a directory (or a file). Dismisses with the path as a string, or
    None when cancelled — the same "None means leave it alone" convention as
    `PickPage`."""

    SCROLLS = False

    def __init__(self, title: str, subtitle: str = "", start: str = "", *,
                 dirs_only: bool = True, suffixes: "tuple[str, ...]" = ()):
        super().__init__()
        self.TITLE = title
        self.subtitle = subtitle or "browse, or type a path and press enter"
        self._dirs_only = dirs_only
        self._suffixes = suffixes
        self._root, self._selected = _resolve_start(start)

    def compose_body(self) -> ComposeResult:
        with Horizontal(id="path-row"):
            yield Input(value=str(self._selected), placeholder="/path/to/…",
                        id="path-input", compact=True)
            yield Button("↑ Up", id="btn-up", compact=True)
        yield _FilteredTree(
            self._root, dirs_only=self._dirs_only, suffixes=self._suffixes, id="path-tree",
        )
        yield Label("", id="path-status")

    def compose_actions(self) -> ComposeResult:
        yield Button("Cancel", id="btn-cancel", compact=True)
        yield Button("Use this", id="btn-use", variant="primary", compact=True)

    def on_mount(self) -> None:
        self.query_one("#path-tree", DirectoryTree).focus()

    # ── keeping the box and the tree in step ─────────────────────────────────

    @on(DirectoryTree.NodeHighlighted, "#path-tree")
    def _highlighted(self, event) -> None:
        entry = event.node.data
        if entry is not None:
            self._set_selected(entry.path)

    @on(DirectoryTree.FileSelected, "#path-tree")
    def _file_selected(self, event) -> None:
        self._set_selected(event.path)
        if not self._dirs_only:
            self.dismiss(str(event.path))

    @on(DirectoryTree.DirectorySelected, "#path-tree")
    def _dir_selected(self, event) -> None:
        # Selecting expands (Tree's own behaviour); this only records it as the
        # thing "Use this" would return.
        self._set_selected(event.path)

    def _set_selected(self, path: Path) -> None:
        self._selected = path
        self.query_one("#path-input", Input).value = str(path)

    @on(Input.Submitted, "#path-input")
    def _typed(self, event: "Input.Submitted") -> None:
        """A typed path re-roots the tree there, so browsing can continue."""
        raw = event.value.strip()
        if not raw:
            return
        path = Path(raw).expanduser()
        if not disk_io.ops.exists(path):
            self.query_one("#path-status", Label).update(
                f"[$mmw-error]✗ {escape(str(path))} does not exist[/]"
            )
            return
        self.query_one("#path-status", Label).update("")
        self._reroot(path if disk_io.ops.is_dir(path) else path.parent)
        self._set_selected(path)

    @on(Button.Pressed, "#btn-up")
    def _up(self, event: "Button.Pressed") -> None:
        event.stop()
        tree = self.query_one("#path-tree", DirectoryTree)
        parent = Path(tree.path).expanduser().resolve().parent
        self._reroot(parent)
        self._set_selected(parent)

    def _reroot(self, path: Path) -> None:
        self.query_one("#path-tree", DirectoryTree).path = path

    # ── result ───────────────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self, event: "Button.Pressed") -> None:
        event.stop()
        self.dismiss(None)

    @on(Button.Pressed, "#btn-use")
    def _use(self, event: "Button.Pressed") -> None:
        event.stop()
        raw = self.query_one("#path-input", Input).value.strip()
        path = Path(raw).expanduser() if raw else self._selected
        if not disk_io.ops.exists(path):
            self.query_one("#path-status", Label).update(
                f"[$mmw-error]✗ {escape(str(path))} does not exist[/]"
            )
            return
        if self._dirs_only and not disk_io.ops.is_dir(path):
            path = path.parent
        self.dismiss(str(path))


def _resolve_start(start: str) -> "tuple[Path, Path]":
    """(root to browse, path to preselect) from whatever the caller had.

    An empty or nonexistent value must still open *somewhere* — falling back to
    the working directory beats an empty tree with no way forward.
    """
    path = Path(start).expanduser() if start.strip() else Path.cwd()
    if not disk_io.ops.exists(path):
        path = Path.cwd()
    return (path, path) if disk_io.ops.is_dir(path) else (path.parent, path)
