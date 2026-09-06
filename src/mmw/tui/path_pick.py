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

from rich.markup import escape as _escape
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DirectoryTree, Input, Label

from mmw.tui.widgets import Button


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
                if p.is_dir():
                    out.append(p)
                elif not self._dirs_only and (
                    not self._suffixes or p.suffix.lower() in self._suffixes
                ):
                    out.append(p)
            except OSError:
                continue        # broken symlink or unreadable — just skip it
        return out


class PathPickModal(ModalScreen):
    """Pick a directory (or a file). Dismisses with the path as a string, or
    None when cancelled — the same "None means leave it alone" convention as
    `PickModal`."""

    DEFAULT_CSS = """
    PathPickModal { align: center middle; }
    PathPickModal > Vertical {
        width: 92;
        max-width: 96%;
        height: 84%;
        max-height: 92%;
        border: double $primary;
        background: $surface;
    }
    PathPickModal #path-title {
        height: 1;
        background: $primary-darken-3;
        padding: 0 2;
        color: $text;
        text-style: bold;
    }
    PathPickModal #path-subtitle {
        height: 1;
        padding: 0 2;
        color: $text-muted;
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    PathPickModal #path-row { height: 3; padding: 0 2; }
    PathPickModal #path-row Input { width: 1fr; }
    PathPickModal #path-row Button { width: auto; min-width: 6; margin-left: 1; }
    PathPickModal DirectoryTree { height: 1fr; margin: 0 2; border: solid $primary-darken-2; }
    PathPickModal #path-status { height: 1; padding: 0 2; color: $text-muted; }
    PathPickModal #path-bar {
        height: auto;
        align: right middle;
        padding: 0 2;
        background: $surface;
        border-top: solid $primary-darken-2;
        overflow-x: auto;
    }
    PathPickModal #path-bar Button { margin-left: 1; min-width: 0; width: auto; }
    PathPickModal #path-bar .spacer { width: 1fr; height: 1; }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, subtitle: str = "", start: str = "", *,
                 dirs_only: bool = True, suffixes: "tuple[str, ...]" = ()):
        super().__init__()
        self._title = title
        self._subtitle = subtitle
        self._dirs_only = dirs_only
        self._suffixes = suffixes
        self._root, self._selected = _resolve_start(start)

    def compose(self) -> ComposeResult:
        hint = "browse, or type a path and press enter"
        with Vertical():
            yield Label(f"  {_escape(self._title)}", id="path-title")
            yield Label(f"  {_escape(self._subtitle or hint)}", id="path-subtitle")
            with Horizontal(id="path-row"):
                yield Input(value=str(self._selected), placeholder="/path/to/…", id="path-input")
                yield Button("↑ Up", id="btn-up", variant="default")
            yield _FilteredTree(
                self._root, dirs_only=self._dirs_only, suffixes=self._suffixes, id="path-tree",
            )
            yield Label("", id="path-status")
            with Horizontal(id="path-bar"):
                yield Label("", classes="spacer")
                yield Button("Cancel", id="btn-cancel", variant="default")
                yield Button("Use this", id="btn-use", variant="primary")

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
        if not path.exists():
            self.query_one("#path-status", Label).update(
                f"  [red]✗ {_escape(str(path))} does not exist[/]"
            )
            return
        self.query_one("#path-status", Label).update("")
        self._reroot(path if path.is_dir() else path.parent)
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

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self, event: "Button.Pressed") -> None:
        event.stop()
        self.dismiss(None)

    @on(Button.Pressed, "#btn-use")
    def _use(self, event: "Button.Pressed") -> None:
        event.stop()
        raw = self.query_one("#path-input", Input).value.strip()
        path = Path(raw).expanduser() if raw else self._selected
        if not path.exists():
            self.query_one("#path-status", Label).update(
                f"  [red]✗ {_escape(str(path))} does not exist[/]"
            )
            return
        if self._dirs_only and not path.is_dir():
            path = path.parent
        self.dismiss(str(path))


def _resolve_start(start: str) -> "tuple[Path, Path]":
    """(root to browse, path to preselect) from whatever the caller had.

    An empty or nonexistent value must still open *somewhere* — falling back to
    the working directory beats an empty tree with no way forward.
    """
    path = Path(start).expanduser() if start.strip() else Path.cwd()
    if not path.exists():
        path = Path.cwd()
    return (path, path) if path.is_dir() else (path.parent, path)
