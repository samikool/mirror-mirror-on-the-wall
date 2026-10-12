"""The bulk review pane and the key modal, driven headlessly.

`cat /etc/apt/sources.list.d/*` names a dozen repositories; the pane is how you
say yes to eight of them and no to four, and how the keys those configs only
*named* get supplied.
"""
import asyncio

from textual.app import App, ComposeResult
from textual.widgets import Button, Checkbox, Input, Label, OptionList, SelectionList, TextArea

from mmw.core import discover
from mmw.core.apt_sources import parse_apt_sources
from mmw.core.draft import drafts_from_entries
from mmw.tui.bulk_review import BulkReviewPage, RepoRow
from mmw.tui.key_modal import KeyPage

ARMOUR = "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nmQINBGXf\n-----END PGP PUBLIC KEY BLOCK-----\n"

BUNDLE = """\
Types: deb
URIs: http://a.example.com/ubuntu
Suites: noble
Components: main
Signed-By: /usr/share/keyrings/a.gpg

Types: deb
URIs: http://b.example.com/ubuntu
Suites: jammy
Components: main
Architectures: arm64

Types: deb
URIs: http://c.example.com/ubuntu
Suites: noble
Components: main
"""


def _stub(monkeypatch, suites=("noble", "jammy"), components=("main", "universe"),
          arches=("amd64", "arm64")):
    monkeypatch.setattr(discover, "list_suites", lambda url: list(suites))
    monkeypatch.setattr(discover, "list_components", lambda url, s: list(components))
    monkeypatch.setattr(discover, "list_arches", lambda url, s, c: list(arches))
    monkeypatch.setattr(discover, "release_dimensions", lambda url, s: ([], []))


class _Harness(App):
    def __init__(self, screen):
        super().__init__()
        self._screen = screen
        self.result = "unset"

    def compose(self) -> ComposeResult:
        yield from ()

    def on_mount(self):
        self.push_screen(self._screen, lambda r: setattr(self, "result", r))


async def _settle(pilot, tries: int = 25):
    for _ in range(tries):
        await pilot.pause()


def _run(scenario):
    asyncio.run(scenario())


def _drafts():
    return drafts_from_entries(parse_apt_sources(BUNDLE))


# ── the pane itself ──────────────────────────────────────────────────────────

def test_every_repository_gets_a_row():
    async def scenario():
        app = _Harness(BulkReviewPage(_drafts()))
        async with app.run_test() as pilot:
            await _settle(pilot)
            rows = list(app.screen.query(RepoRow))
            assert [r.draft.url for r in rows] == [
                "http://a.example.com/ubuntu",
                "http://b.example.com/ubuntu",
                "http://c.example.com/ubuntu",
            ]
            assert all(r.included for r in rows), "everything ticked by default"

    _run(scenario)


def test_unticking_a_row_drops_it_from_the_result():
    async def scenario():
        app = _Harness(BulkReviewPage(_drafts()))
        async with app.run_test() as pilot:
            await _settle(pilot)
            screen = app.screen
            screen.query_one("#repo-1-on", Checkbox).value = False
            await pilot.pause()
            screen.query_one("#btn-done", Button).press()
            await _settle(pilot)

        assert [d.url for d in app.result] == [
            "http://a.example.com/ubuntu", "http://c.example.com/ubuntu"]

    _run(scenario)


def test_cancelling_keeps_everything_untouched():
    """None means "leave it alone" — the same convention as PickPage."""
    async def scenario():
        app = _Harness(BulkReviewPage(_drafts()))
        async with app.run_test() as pilot:
            await _settle(pilot)
            app.screen.query_one("#btn-cancel", Button).press()
            await _settle(pilot)

        assert app.result is None

    _run(scenario)


def test_clear_then_select_all_round_trips():
    async def scenario():
        app = _Harness(BulkReviewPage(_drafts()))
        async with app.run_test() as pilot:
            await _settle(pilot)
            screen = app.screen
            screen.query_one("#btn-none", Button).press()
            await _settle(pilot)
            assert not any(r.included for r in screen.query(RepoRow))
            screen.query_one("#btn-all", Button).press()
            await _settle(pilot)
            assert all(r.included for r in screen.query(RepoRow))

    _run(scenario)


def test_a_row_matching_a_configured_repository_is_marked_as_a_merge():
    """`merge_sources()` keys on the URI, so this row widens an existing source
    rather than adding a second one — worth saying before Add is pressed."""
    async def scenario():
        drafts = _drafts()
        app = _Harness(BulkReviewPage(drafts, {drafts[0].identity}))
        async with app.run_test() as pilot:
            await _settle(pilot)
            tags = [str(lbl.render()) for lbl in app.screen.query(".repo-tag")]

        assert "merge" in tags[0] and "new" in tags[1]

    _run(scenario)


def test_a_row_drills_into_the_same_pick_modal(monkeypatch):
    _stub(monkeypatch)

    async def scenario():
        drafts = _drafts()
        for d in drafts:
            d.probe()
        app = _Harness(BulkReviewPage(drafts))
        async with app.run_test() as pilot:
            await _settle(pilot)
            app.screen.query_one("#repo-1-btn-arches", Button).press()
            await _settle(pilot)

            lst = app.screen.query_one("#pick-list", SelectionList)
            assert set(lst.selected) == {"arm64"}, "the config's filter is preselected"
            lst.select("amd64")
            await pilot.pause()
            app.screen.query_one("#btn-done", Button).press()
            await _settle(pilot)

            assert drafts[1].chosen["arches"] == ["arm64", "amd64"]

    _run(scenario)


# ── keys ─────────────────────────────────────────────────────────────────────

def test_scanning_a_directory_fills_in_the_named_keyrings(tmp_path):
    (tmp_path / "a.gpg").write_text(ARMOUR)

    async def scenario():
        drafts = _drafts()
        app = _Harness(BulkReviewPage(drafts))
        async with app.run_test() as pilot:
            await _settle(pilot)
            app.screen.query_one("#keydir", Input).value = str(tmp_path)
            app.screen.query_one("#btn-keydir", Button).press()
            await _settle(pilot, 60)

        assert drafts[0].gpg_key == ARMOUR and drafts[0].key_source == "dir"
        assert drafts[1].gpg_key is None, "a repo naming no keyring is left alone"

    _run(scenario)


def test_the_key_modal_loads_a_scanned_file(tmp_path):
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "a.gpg").write_text(ARMOUR)

    async def scenario():
        app = _Harness(KeyPage("repo-a", "a.gpg", None, str(tmp_path)))
        async with app.run_test() as pilot:
            await _settle(pilot, 60)
            found = app.screen.query_one("#key-found", OptionList)
            assert found.display is True
            found.highlighted = 0
            found.action_select()
            await _settle(pilot)
            app.screen.query_one("#btn-save", Button).press()
            await _settle(pilot)

        armour, name, url = app.result
        assert "BEGIN PGP PUBLIC KEY BLOCK" in armour and name == "a.gpg" and url is None

    _run(scenario)


def test_the_key_modal_refuses_text_that_is_not_a_key():
    async def scenario():
        app = _Harness(KeyPage("repo-a", "a.gpg"))
        async with app.run_test() as pilot:
            await _settle(pilot)
            app.screen.query_one("#key-area", TextArea).text = "just some text"
            app.screen.query_one("#btn-save", Button).press()
            await _settle(pilot)

            assert app.result == "unset", "the modal must stay open"

    _run(scenario)


def test_the_key_modal_can_clear_a_key():
    async def scenario():
        app = _Harness(KeyPage("repo-a", "a.gpg", ARMOUR))
        async with app.run_test() as pilot:
            await _settle(pilot)
            app.screen.query_one("#btn-clear", Button).press()
            await _settle(pilot)

        assert app.result == ("", None, None)

    _run(scenario)


# ── browsing for the key directory, instead of typing it ─────────────────────

def test_browsing_sets_the_key_directory_and_scans_it(tmp_path):
    """The Browse button and the text box are the same input — picking a path
    fills the box and runs the scan, so there is no second way to be wrong."""
    from textual.widgets import Button as TButton

    from mmw.tui.path_pick import PathPickPage
    (tmp_path / "a.gpg").write_text(ARMOUR)

    async def scenario():
        drafts = _drafts()
        app = _Harness(BulkReviewPage(drafts))
        async with app.run_test() as pilot:
            await _settle(pilot)
            app.screen.query_one("#btn-keydir-browse", TButton).press()
            await _settle(pilot)

            picker = pilot.app.screen
            assert isinstance(picker, PathPickPage)
            picker.query_one("#path-input", Input).value = str(tmp_path)
            picker.query_one("#btn-use", TButton).press()
            await _settle(pilot, 60)

            assert app.screen.query_one("#keydir", Input).value == str(tmp_path)

        assert drafts[0].gpg_key == ARMOUR, "picking must also scan"

    _run(scenario)


def test_cancelling_the_browser_leaves_the_key_directory_alone(tmp_path):
    from textual.widgets import Button as TButton

    async def scenario():
        app = _Harness(BulkReviewPage(_drafts()))
        async with app.run_test() as pilot:
            await _settle(pilot)
            app.screen.query_one("#keydir", Input).value = "/already/typed"
            app.screen.query_one("#btn-keydir-browse", TButton).press()
            await _settle(pilot)
            pilot.app.screen.query_one("#btn-cancel", TButton).press()
            await _settle(pilot)

            assert app.screen.query_one("#keydir", Input).value == "/already/typed"

    _run(scenario)


def test_the_key_modal_can_fetch_from_a_url(monkeypatch):
    from mmw.core import keys

    monkeypatch.setattr(keys, "fetch_key", lambda url, client: ARMOUR)

    async def scenario():
        app = _Harness(KeyPage("repo-a", "a.gpg"))
        async with app.run_test() as pilot:
            await _settle(pilot)
            app.screen.query_one("#key-url", Input).value = "https://nvidia.example/gpgkey"
            app.screen.query_one("#btn-fetch", Button).press()
            await _settle(pilot, 60)

            assert app.screen.query_one("#key-area", TextArea).text == ARMOUR
            app.screen.query_one("#btn-save", Button).press()
            await _settle(pilot)

        armour, name, url = app.result
        assert armour == ARMOUR.strip()
        # Kept, so sync mirrors the key at its own upstream path and a vendor's
        # `curl …/gpgkey` install line works against the mirror.
        assert url == "https://nvidia.example/gpgkey"

    _run(scenario)


def test_editing_a_fetched_key_drops_its_url(monkeypatch):
    """The URL describes what was fetched; once the box says something else
    it would publish a key the repository is not configured with."""
    from mmw.core import keys

    monkeypatch.setattr(keys, "fetch_key", lambda url, client: ARMOUR)

    async def scenario():
        app = _Harness(KeyPage("repo-a", "a.gpg"))
        async with app.run_test() as pilot:
            await _settle(pilot)
            app.screen.query_one("#key-url", Input).value = "https://nvidia.example/gpgkey"
            app.screen.query_one("#btn-fetch", Button).press()
            await _settle(pilot, 60)
            other = ARMOUR.replace("mQINBG", "mQINBH") if "mQINBG" in ARMOUR else ARMOUR + "\n"
            app.screen.query_one("#key-area", TextArea).text = other.strip() + " "
            app.screen.query_one("#btn-save", Button).press()
            await _settle(pilot)

        assert app.result[2] is None

    _run(scenario)


def test_a_key_fetched_in_bulk_review_keeps_its_url_into_the_config():
    from mmw.core.draft import SourceDraft

    d = SourceDraft(url="https://nvidia.example/libnvidia-container/stable/deb/amd64",
                        provider="apt-flat", name="nv")
    d.set_key(ARMOUR, source="fetched", url="https://nvidia.example/libnvidia-container/gpgkey")
    src = d.to_source()
    assert src.gpg_key_url == "https://nvidia.example/libnvidia-container/gpgkey"
    assert SourceDraft.from_source(src).gpg_key_url == src.gpg_key_url
    d.set_key(ARMOUR, source="pasted")             # replaced by a paste: no URL any more
    assert d.to_source().gpg_key_url is None


def test_the_key_modal_reports_a_failed_fetch_and_does_not_advance(monkeypatch):
    from mmw.core import keys

    def boom(url, client):
        raise RuntimeError("HTTP 404")

    monkeypatch.setattr(keys, "fetch_key", boom)

    async def scenario():
        app = _Harness(KeyPage("repo-a", "a.gpg"))
        async with app.run_test() as pilot:
            await _settle(pilot)
            app.screen.query_one("#key-url", Input).value = "https://nvidia.example/gpgkey"
            app.screen.query_one("#btn-fetch", Button).press()
            await _settle(pilot, 60)

            assert app.screen.query_one("#key-area", TextArea).text == ""
            assert "404" in str(app.screen.query_one("#key-status", Label).content)

    _run(scenario)


def test_the_key_modal_can_browse_too(tmp_path):
    from textual.widgets import Button as TButton

    (tmp_path / "a.gpg").write_text(ARMOUR)

    async def scenario():
        app = _Harness(KeyPage("repo-a", "a.gpg"))
        async with app.run_test() as pilot:
            await _settle(pilot)
            app.screen.query_one("#btn-browse", TButton).press()
            await _settle(pilot)

            picker = pilot.app.screen
            picker.query_one("#path-input", Input).value = str(tmp_path)
            picker.query_one("#btn-use", TButton).press()
            await _settle(pilot, 60)

            assert app.screen.query_one("#key-dir", Input).value == str(tmp_path)
            assert app.screen.query_one("#key-found", OptionList).display is True

    _run(scenario)


def test_row_summary_is_positional_and_fits_a_narrow_row():
    """The per-row dimension line must fit an 80-column terminal in every state.

    It previously named each dimension — "suites all — resolved at sync  ·
    components all — …" — which ran to 108 columns against roughly 64
    available, so it truncated in the *ordinary* state of a freshly pasted
    repository, not an edge case. The three buttons directly above the line
    are already labelled Suites…/Components…/Architectures…, so the names were
    restating what was on screen one line up.
    """
    import re

    from mmw.core.draft import DIMENSIONS, SourceDraft
    from mmw.tui.bulk_review import RepoRow

    def rendered(draft):
        row = RepoRow.__new__(RepoRow)
        row.draft = draft
        return re.sub(r"\[/?[^\]]*\]", "", RepoRow._dimension_text(row))

    # Everything open — said once, not three times.
    d = SourceDraft(url="https://r.example.com/ubuntu")
    assert rendered(d) == "all — resolved at sync"

    # Narrowed on one axis only: the other two still render, so `arm64` never
    # moves out of third place. Positional labelling needs fixed positions.
    d2 = SourceDraft(url="https://r.example.com/ubuntu")
    d2.set_dimension("suites", ["noble", "jammy"], chosen=["noble"], enumerated=True)
    text = rendered(d2)
    assert text.count("·") == len(DIMENSIONS) - 1, f"a slot went missing: {text!r}"
    assert text.startswith("noble")
    assert text.endswith("all")

    # A long selection is abbreviated rather than allowed to run away.
    d3 = SourceDraft(url="https://r.example.com/ubuntu")
    d3.set_dimension("suites", [f"s{i}" for i in range(9)],
                     chosen=[f"s{i}" for i in range(7)], enumerated=True)
    assert "+5" in rendered(d3)

    # 64 is what the row actually gets at an 80-column terminal.
    for draft in (d, d2, d3):
        assert len(rendered(draft)) <= 64, f"{rendered(draft)!r} is too wide"


def test_key_page_is_a_scrolling_page_with_one_row_controls():
    import asyncio

    from mmw.tui.key_modal import KeyPage
    from mmw.tui.ui.base import ThemedApp
    from mmw.tui.ui.page import Page

    assert issubclass(KeyPage, Page)

    class _Host(ThemedApp):
        def on_mount(self):
            self.push_screen(KeyPage("debian", "debian-archive.gpg"))

    async def go():
        app = _Host()
        async with app.run_test(size=(80, 24)) as pilot:
            for _ in range(3):
                await pilot.pause()
            assert app.screen.TITLE == "GPG key — debian"
            assert all(w.region.height == 1 for w in app.screen.query("Input, Button"))
    asyncio.run(go())


def test_bulk_review_page_rows_escape_markup_and_controls_are_one_row():
    import asyncio

    from mmw.core.draft import SourceDraft
    from mmw.tui.bulk_review import BulkReviewPage
    from mmw.tui.ui.base import ThemedApp
    from mmw.tui.ui.page import Page

    assert issubclass(BulkReviewPage, Page)
    drafts = [SourceDraft(url="https://r.example.com/x[b]y")]

    class _Host(ThemedApp):
        def on_mount(self):
            self.push_screen(BulkReviewPage(drafts))

    async def go():
        app = _Host()
        async with app.run_test(size=(80, 24)) as pilot:
            for _ in range(3):
                await pilot.pause()
            assert "x[b]y" in str(app.screen.query_one(".repo-url").render())
            assert all(b.region.height == 1 for b in app.screen.query("Button"))
    asyncio.run(go())


def test_a_key_fetched_while_editing_a_repository_sets_its_key_url(monkeypatch):
    """The reported bug: nvidia.github.io re-added, key fetched from
    …/libnvidia-container/gpgkey — saved as pasted text with no gpg_key_url, so
    nothing was mirrored at the key's own path."""
    from mmw.core import config, keys
    from mmw.tui.source_screens import EditSourcePage

    url = "https://nvidia.example/libnvidia-container/gpgkey"
    monkeypatch.setattr(keys, "fetch_key", lambda u, client: ARMOUR)
    src = config.SourceConfig(url="https://nvidia.example/libnvidia-container/stable/deb/amd64",
                              provider="apt-flat", name="nv", label="nvidia")

    async def scenario():
        app = _Harness(EditSourcePage(src))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#btn-key", Button).press()
            await _settle(pilot)
            app.screen.query_one("#key-url", Input).value = url
            app.screen.query_one("#btn-fetch", Button).press()
            await _settle(pilot, 60)
            app.screen.query_one("#btn-save", Button).press()          # key page
            await _settle(pilot)
            assert app.screen.query_one("#edit-source-key-url", Input).value == url
            app.screen.query_one("#btn-save-source", Button).press()
            await _settle(pilot)

        assert app.result.gpg_key_url == url
        assert app.result.gpg_key == ARMOUR.strip()     # and an offline copy

    _run(scenario)


def _edit_with_key_page(src, on_key_page):
    from mmw.tui.source_screens import EditSourcePage

    async def scenario():
        app = _Harness(EditSourcePage(src))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#btn-key", Button).press()
            await _settle(pilot)
            on_key_page(app.screen)
            await _settle(pilot)
            app.screen.query_one("#btn-save-source", Button).press()
            await _settle(pilot)
        return app.result
    return asyncio.run(scenario())


def test_clearing_the_key_also_clears_its_url():
    """The URL *is* a key: left in place, sync fetches it again and the key
    the user just cleared comes straight back."""
    from mmw.core import config
    src = config.SourceConfig(url="https://nvidia.example/deb/amd64", provider="apt-flat",
                              name="nv", label="nvidia", gpg_key=ARMOUR.strip(),
                              gpg_key_url="https://nvidia.example/gpgkey")
    result = _edit_with_key_page(src, lambda s: s.query_one("#btn-clear", Button).press())
    assert result.gpg_key is None and result.gpg_key_url is None


def test_a_key_configured_only_by_url_shows_as_set():
    from mmw.core import config
    from mmw.tui.source_screens import EditSourcePage
    src = config.SourceConfig(url="https://nvidia.example/deb/amd64", provider="apt-flat",
                              name="nv", label="nvidia", gpg_key_url="https://nvidia.example/gpgkey")

    async def scenario():
        app = _Harness(EditSourcePage(src))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            return str(app.screen.query_one("#key-status").render())
    shown = asyncio.run(scenario())
    assert "no key" not in shown and "gpgkey" in shown
