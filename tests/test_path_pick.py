"""`PathPickPage` — browsing for a path instead of typing it.

Every path this TUI asks for lives somewhere the user has to go and look, and a
typo surfaces only as "no key files found". These drive the modal headlessly.
"""
import asyncio

from textual.app import App, ComposeResult
from textual.widgets import Button, DirectoryTree, Input, Label

from mmw.tui.path_pick import PathPickPage, _resolve_start
from waiting import settle


class _Harness(App):
    def __init__(self, screen):
        super().__init__()
        self._screen = screen
        self.result = "unset"

    def compose(self) -> ComposeResult:
        yield from ()

    def on_mount(self):
        self.push_screen(self._screen, lambda r: setattr(self, "result", r))


async def _settle(pilot, tries: int = 30):
    """Until no work is in flight — see tests/waiting.py. The count is ignored."""
    await settle(pilot)


def _run(scenario):
    asyncio.run(scenario())


def _tree(tmp_path):
    (tmp_path / "keys" / "nested").mkdir(parents=True)
    (tmp_path / "keys" / "a.gpg").write_text("x")
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "b.sources").write_text("x")
    return tmp_path


# ── where it opens ───────────────────────────────────────────────────────────

def test_a_directory_start_roots_there(tmp_path):
    root, selected = _resolve_start(str(tmp_path))
    assert root == tmp_path and selected == tmp_path


def test_a_file_start_roots_at_its_parent(tmp_path):
    f = tmp_path / "x.sources"
    f.write_text("x")
    root, selected = _resolve_start(str(f))
    assert root == tmp_path and selected == f


def test_a_nonexistent_start_still_opens_somewhere(tmp_path):
    """An empty tree with no way forward is worse than the wrong directory."""
    root, _ = _resolve_start(str(tmp_path / "nope"))
    assert root.is_dir()


# ── browsing ─────────────────────────────────────────────────────────────────

def test_directories_are_shown_and_dotfiles_are_not(tmp_path):
    _tree(tmp_path)

    async def scenario():
        app = _Harness(PathPickPage("Pick", start=str(tmp_path)))
        async with app.run_test() as pilot:
            await _settle(pilot)
            names = [str(n.label) for n in app.screen.query_one(DirectoryTree).root.children]

        assert "keys" in names
        assert ".hidden" not in names
        assert "notes.txt" not in names, "dirs_only hides files"

    _run(scenario)


def test_files_of_interest_are_shown_when_asked(tmp_path):
    _tree(tmp_path)

    async def scenario():
        app = _Harness(PathPickPage(
            "Pick", start=str(tmp_path), dirs_only=False, suffixes=(".sources",)))
        async with app.run_test() as pilot:
            await _settle(pilot)
            names = [str(n.label) for n in app.screen.query_one(DirectoryTree).root.children]

        assert "b.sources" in names and "keys" in names
        assert "notes.txt" not in names, "a suffix filter still applies"

    _run(scenario)


def test_typing_a_path_reroots_the_tree(tmp_path):
    _tree(tmp_path)

    async def scenario():
        app = _Harness(PathPickPage("Pick", start=str(tmp_path)))
        async with app.run_test() as pilot:
            await _settle(pilot)
            inp = app.screen.query_one("#path-input", Input)
            inp.value = str(tmp_path / "keys")
            await pilot.pause()
            inp.post_message(Input.Submitted(inp, inp.value))
            await _settle(pilot)

            tree = app.screen.query_one(DirectoryTree)
            assert tree.path == tmp_path / "keys"
            assert "nested" in [str(n.label) for n in tree.root.children]

    _run(scenario)


def test_a_typed_path_that_does_not_exist_is_reported_not_crashed(tmp_path):
    async def scenario():
        app = _Harness(PathPickPage("Pick", start=str(tmp_path)))
        async with app.run_test() as pilot:
            await _settle(pilot)
            inp = app.screen.query_one("#path-input", Input)
            inp.value = str(tmp_path / "nope")
            inp.post_message(Input.Submitted(inp, inp.value))
            await _settle(pilot)

            assert "does not exist" in str(app.screen.query_one("#path-status", Label).render())
            assert app.screen.query_one(DirectoryTree).path == tmp_path

    _run(scenario)


def test_up_reroots_to_the_parent(tmp_path):
    _tree(tmp_path)

    async def scenario():
        app = _Harness(PathPickPage("Pick", start=str(tmp_path / "keys")))
        async with app.run_test() as pilot:
            await _settle(pilot)
            app.screen.query_one("#btn-up", Button).press()
            await _settle(pilot)

            assert app.screen.query_one(DirectoryTree).path == tmp_path.resolve()

    _run(scenario)


# ── result ───────────────────────────────────────────────────────────────────

def test_use_this_returns_the_current_path(tmp_path):
    _tree(tmp_path)

    async def scenario():
        app = _Harness(PathPickPage("Pick", start=str(tmp_path)))
        async with app.run_test() as pilot:
            await _settle(pilot)
            app.screen.query_one("#path-input", Input).value = str(tmp_path / "keys")
            app.screen.query_one("#btn-use", Button).press()
            await _settle(pilot)

        assert app.result == str(tmp_path / "keys")

    _run(scenario)


def test_use_this_on_a_file_returns_its_directory_in_dirs_only_mode(tmp_path):
    _tree(tmp_path)

    async def scenario():
        app = _Harness(PathPickPage("Pick", start=str(tmp_path)))
        async with app.run_test() as pilot:
            await _settle(pilot)
            app.screen.query_one("#path-input", Input).value = str(tmp_path / "notes.txt")
            app.screen.query_one("#btn-use", Button).press()
            await _settle(pilot)

        assert app.result == str(tmp_path)

    _run(scenario)


def test_cancelling_returns_none(tmp_path):
    async def scenario():
        app = _Harness(PathPickPage("Pick", start=str(tmp_path)))
        async with app.run_test() as pilot:
            await _settle(pilot)
            app.screen.query_one("#btn-cancel", Button).press()
            await _settle(pilot)

        assert app.result is None

    _run(scenario)


def test_path_pick_page_is_a_page():
    from mmw.tui.path_pick import PathPickPage
    from mmw.tui.ui.page import Page
    assert issubclass(PathPickPage, Page) and PathPickPage.SCROLLS is False


# ── a place that isn't a local folder yet: Add root ─────────────────────────

def _pick(tmp_path, typed, *, enter=False, **kw):
    async def scenario():
        app = _Harness(PathPickPage("Add a root", start=str(tmp_path), **kw))
        async with app.run_test() as pilot:
            await _settle(pilot)
            box = app.screen.query_one("#path-input", Input)
            box.value = typed
            if enter:
                box.focus()                  # where a person types it
                await pilot.press("enter")
                await _settle(pilot)
            if app.result == "unset":
                app.screen.query_one("#btn-use", Button).press()
                await _settle(pilot)
            status = str(app.screen.query_one("#path-status", Label).render()) if app.result == "unset" else ""
            return app.result, status
    return asyncio.run(scenario())


def test_an_ssh_location_is_taken_as_typed_when_remote_is_allowed(tmp_path):
    for enter in (False, True):
        result, _ = _pick(tmp_path, "sam-server-remote:/12-tb/mirrors", enter=enter, remote=True, new=True)
        assert result == "sam-server-remote:/12-tb/mirrors", enter


def test_an_ssh_location_is_refused_where_only_local_paths_make_sense(tmp_path):
    result, status = _pick(tmp_path, "sam-server-remote:/12-tb/mirrors")
    assert result == "unset" and "does not exist" in status


def test_a_new_folder_is_taken_when_new_is_allowed(tmp_path):
    result, _ = _pick(tmp_path, str(tmp_path / "fresh"), remote=True, new=True)
    assert result == str(tmp_path / "fresh")
    result, status = _pick(tmp_path, str(tmp_path / "no" / "such" / "place"), remote=True, new=True)
    assert result == "unset" and "no/such" in status            # its parent has to be there


def test_closing_straight_after_opening_never_trips_on_the_tree(tmp_path):
    # The tree reports highlights for a moment after mounting; Esc in that
    # moment used to meet a page with no path box left.
    root = _tree(tmp_path)

    async def scenario():
        for _ in range(5):
            app = _Harness(PathPickPage("Pick", start=str(root)))
            async with app.run_test() as pilot:
                app.screen.dismiss(None)
                await _settle(pilot)
                assert app.result is None
    _run(scenario)
