"""Editing an already-configured repository — the only route to *narrowing*
a selection through the TUI (`config.merge_sources()`:
merging is union-only by design, so re-adding can only ever widen).

Driven headlessly with a Textual pilot; no network — the pickers are seeded
straight from the source's own stored dimensions, so nothing here needs to
probe a repository.
"""
import asyncio

from textual.app import ComposeResult
from textual.widgets import Button, Checkbox, Input, SelectionList

from mmw.core import config
from mmw.tui.ui.base import ThemedApp
from mmw.tui.source_screens import EditSourcePage


def _source(**overrides) -> config.SourceConfig:
    fields = dict(
        url="https://example.com/debian",
        provider="apt",
        name="deb-example",
        suites=["bookworm", "trixie"],
        components=["main", "contrib"],
        arches=["amd64", "arm64"],
    )
    fields.update(overrides)
    return config.SourceConfig(**fields)


async def _settle(pilot, tries: int = 25):
    for _ in range(tries):
        await pilot.pause()


def _host_app(screen):
    class _App(ThemedApp):
        def compose(self) -> ComposeResult:
            yield from ()

        def on_mount(self):
            self.push_screen(screen, self._capture)

        def _capture(self, value):
            self.result = value

    app = _App()
    app.result = None
    return app


async def _pick(pilot, app, dim: str, values: list[str]) -> None:
    """Open a dimension's PickPage (pushed on top of EditSourcePage), tick
    exactly `values`, confirm."""
    app.screen.query_one(f"#btn-{dim}", Button).press()
    await _settle(pilot)
    modal = app.screen
    lst = modal.query_one("#pick-list", SelectionList)
    lst.deselect_all()
    for v in values:
        lst.select(v)
    await pilot.pause()
    modal.query_one("#btn-done", Button).press()
    await _settle(pilot)


def _run_and_get(scenario):
    return asyncio.run(scenario())


def test_escape_returns_none():
    async def scenario():
        app = _host_app(EditSourcePage(_source()))
        async with app.run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await pilot.press("escape")
            await _settle(pilot)
        return app.result

    assert _run_and_get(scenario) is None


def test_saving_unchanged_returns_an_equivalent_source():
    src = _source()

    async def scenario():
        app = _host_app(EditSourcePage(src))
        async with app.run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#btn-save-source", Button).press()
            await _settle(pilot)
        return app.result

    result = _run_and_get(scenario)
    assert result is not None
    assert result.identity == src.identity
    assert result.name == src.name
    assert sorted(result.suites) == sorted(src.suites)
    assert sorted(result.components) == sorted(src.components)
    assert sorted(result.arches) == sorted(src.arches)


def test_narrowing_a_dimension_is_preserved_through_save():
    """The core reason this screen exists: dropping a suite through the
    picker must actually stick, not get widened back by anything downstream."""
    src = _source(suites=["bookworm", "trixie"])

    async def scenario():
        app = _host_app(EditSourcePage(src))
        async with app.run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await _pick(pilot, app, "suites", ["bookworm"])
            app.screen.query_one("#btn-save-source", Button).press()
            await _settle(pilot)
        return app.result

    result = _run_and_get(scenario)
    assert result is not None
    assert result.suites == ["bookworm"]


def test_narrowing_arches_is_preserved_through_save():
    src = _source(arches=["amd64", "arm64"])

    async def scenario():
        app = _host_app(EditSourcePage(src))
        async with app.run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await _pick(pilot, app, "arches", ["arm64"])
            app.screen.query_one("#btn-save-source", Button).press()
            await _settle(pilot)
        return app.result

    result = _run_and_get(scenario)
    assert result is not None
    assert result.arches == ["arm64"]


def _edit(src, actions, existing_names=None, existing_urls=None):
    """Open the editor on `src`, run `actions(app)` (sync, on the screen's
    widgets), press Save, and return what it dismissed with."""
    async def scenario():
        app = _host_app(EditSourcePage(src, existing_names=existing_names,
                                         existing_urls=existing_urls))
        async with app.run_test(size=(100, 50)) as pilot:
            await _settle(pilot)
            actions(app.screen)
            await _settle(pilot)
            app.screen.query_one("#btn-save-source", Button).press()
            await _settle(pilot)
        return app.result
    return _run_and_get(scenario)


def test_url_is_editable():
    """Every mmw.toml setting is reachable from the TUI — the URL included.
    A different URL is a different repository: the old one's pool content
    becomes orphaned and `mmw prune` reclaims it, like a delete-and-re-add."""
    def act(screen):
        screen.query_one("#edit-source-url", Input).value = "https://mirror.example.org/debian/"
    result = _edit(_source(), act)
    assert result is not None
    assert result.url == "https://mirror.example.org/debian"


def test_url_of_another_repository_is_refused():
    def act(screen):
        screen.query_one("#edit-source-url", Input).value = "https://other.example.com/debian"
    assert _edit(_source(), act,
                 existing_urls={config.normalize_url("https://other.example.com/debian")}) is None


def test_label_provider_and_key_url_are_editable():
    def act(screen):
        from textual.widgets import Select
        screen.query_one("#edit-source-label", Input).value = "vendor"
        screen.query_one("#edit-source-provider", Select).value = "apt-flat"
        screen.query_one("#edit-source-key-url", Input).value = "https://example.com/key.asc"
    result = _edit(_source(), act)
    assert result.label == "vendor"
    assert result.provider == "apt-flat"
    assert result.gpg_key_url == "https://example.com/key.asc"


def test_clearing_the_key_url_removes_it():
    def act(screen):
        screen.query_one("#edit-source-key-url", Input).value = "  "
    assert _edit(_source(gpg_key_url="https://example.com/key.asc"), act).gpg_key_url is None


def test_extra_files_are_editable():
    def act(screen):
        from textual.widgets import TextArea
        screen.query_one("#edit-source-extra-files", TextArea).text = (
            "https://example.com/repo.list\n\nhttps://example.com/keys/repo.asc\n")
    result = _edit(_source(), act)
    assert result.extra_files == ["https://example.com/repo.list",
                                  "https://example.com/keys/repo.asc"]


def test_existing_extra_files_are_shown_and_survive():
    src = _source(extra_files=["https://example.com/repo.list"])
    assert _edit(src, lambda screen: None).extra_files == ["https://example.com/repo.list"]


def test_an_unservable_extra_file_is_refused():
    def act(screen):
        from textual.widgets import TextArea
        screen.query_one("#edit-source-extra-files", TextArea).text = "https://example.com/k?x=1\n"
    assert _edit(_source(), act) is None


def test_suite_overrides_are_editable():
    def act(screen):
        from textual.widgets import TextArea
        screen.query_one("#edit-source-overrides", TextArea).text = (
            "bookworm: main\ntrixie: main, contrib\n")
    result = _edit(_source(), act)
    assert [(o.suite, o.components) for o in result.suite_overrides] == [
        ("bookworm", ["main"]), ("trixie", ["main", "contrib"])]


def test_existing_suite_overrides_are_shown_and_survive():
    src = _source(suite_overrides=[config.SuiteOverride(suite="trixie", components=["main"])])
    result = _edit(src, lambda screen: None)
    assert [(o.suite, o.components) for o in result.suite_overrides] == [("trixie", ["main"])]


def test_a_malformed_override_line_is_refused():
    def act(screen):
        from textual.widgets import TextArea
        screen.query_one("#edit-source-overrides", TextArea).text = "just-a-suite\n"
    assert _edit(_source(), act) is None


def test_clearing_stated_suites_to_zero_is_refused():
    """Unticking every suite of a repository that *stated* its suites is the
    add flow's own guard, and it still applies here: the user has narrowed to
    nothing, which mirrors nothing."""
    src = _source()

    async def scenario():
        app = _host_app(EditSourcePage(src))
        async with app.run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await _pick(pilot, app, "suites", [])
            app.screen.query_one("#btn-save-source", Button).press()
            await _settle(pilot)
        return app.result

    result = _run_and_get(scenario)
    assert result is None


def test_dist_upgrade_and_gpg_key_url_survive_an_edit():
    """`SourceDraft.to_source()` has no field for either — both must be
    carried across by the screen itself, or editing silently drops them."""
    src = _source(dist_upgrade=True, gpg_key_url="https://example.com/key.gpg")

    async def scenario():
        app = _host_app(EditSourcePage(src))
        async with app.run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            # Touch an unrelated dimension so this isn't just testing the
            # untouched-save path.
            await _pick(pilot, app, "components", ["main"])
            app.screen.query_one("#btn-save-source", Button).press()
            await _settle(pilot)
        return app.result

    result = _run_and_get(scenario)
    assert result is not None
    assert result.dist_upgrade is True
    assert result.gpg_key_url == "https://example.com/key.gpg"
    assert result.components == ["main"]


def test_include_sources_switch_is_saved():
    src = _source(include_sources=False)

    async def scenario():
        app = _host_app(EditSourcePage(src))
        async with app.run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#include-sources-switch", Checkbox).value = True
            await _settle(pilot)
            app.screen.query_one("#btn-save-source", Button).press()
            await _settle(pilot)
        return app.result

    result = _run_and_get(scenario)
    assert result is not None
    assert result.include_sources is True


def test_a_repository_already_mirroring_all_suites_can_still_be_saved():
    """`suites = []` is the documented "empty means all" state, not an error.

    It is what `mmw add` writes whenever the suites are left open, and
    `resolve_source()` fills it in by listing `dists/` at sync time. A guard
    that merely tested for emptiness made such a repository impossible to edit
    at all — Save silently did nothing and the modal just sat there, which
    reads as a broken button rather than a refusal.
    """
    src = config.SourceConfig(url="https://deb.example.com/debian", components=["main"])
    assert src.suites == []

    async def scenario():
        app = _host_app(EditSourcePage(src))
        async with app.run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#btn-save-source", Button).press()
            await _settle(pilot)
        return app.result

    result = _run_and_get(scenario)
    assert result is not None, "an all-suites repository must be saveable"
    assert result.suites == []          # still "all", not materialized
    assert result.identity == src.identity


def test_edit_source_page_fields_are_one_row_and_name_clash_is_refused_in_place():
    from mmw.tui.source_screens import EditSourcePage

    src = config.SourceConfig(url="https://deb.example.com/debian", suites=["bookworm"],
                              components=["main"], arches=["amd64"])
    result = []

    class _Host(ThemedApp):
        def on_mount(self):
            self.push_screen(EditSourcePage(src, existing_names={src.name, "taken"}),
                             result.append)

    async def go():
        app = _Host()
        async with app.run_test(size=(80, 24)) as pilot:
            for _ in range(3):
                await pilot.pause()
            page = app.screen
            assert all(w.region.height == 1 for w in page.query("Input, Button, Checkbox"))
            page.query_one("#edit-source-name").value = "taken"
            page.query_one("#btn-save-source").press()
            for _ in range(3):
                await pilot.pause()
            assert "already the name" in str(page.query_one("#page-status").render())
            assert app.focused.id == "edit-source-name"
            assert result == []
    asyncio.run(go())
