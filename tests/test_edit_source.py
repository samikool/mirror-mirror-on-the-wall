"""Editing an already-configured repository — the only route to *narrowing*
a selection through the TUI (see design-notes.md's `config.merge_sources()` note:
merging is union-only by design, so re-adding can only ever widen).

Driven headlessly with a Textual pilot; no network — the pickers are seeded
straight from the source's own stored dimensions, so nothing here needs to
probe a repository.
"""
import asyncio

from textual.app import App, ComposeResult
from textual.widgets import Button, Input, Label, SelectionList, Switch

from mmw.core import config
from mmw.tui.source_screens import EditSourceScreen


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
    class _App(App):
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
    """Open a dimension's PickModal (pushed on top of EditSourceScreen), tick
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
        app = _host_app(EditSourceScreen(_source()))
        async with app.run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await pilot.press("escape")
            await _settle(pilot)
        return app.result

    assert _run_and_get(scenario) is None


def test_saving_unchanged_returns_an_equivalent_source():
    src = _source()

    async def scenario():
        app = _host_app(EditSourceScreen(src))
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
        app = _host_app(EditSourceScreen(src))
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
        app = _host_app(EditSourceScreen(src))
        async with app.run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await _pick(pilot, app, "arches", ["arm64"])
            app.screen.query_one("#btn-save-source", Button).press()
            await _settle(pilot)
        return app.result

    result = _run_and_get(scenario)
    assert result is not None
    assert result.arches == ["arm64"]


def test_url_widget_is_not_an_input():
    async def scenario():
        app = _host_app(EditSourceScreen(_source()))
        async with app.run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            widget = app.screen.query_one("#edit-source-url")
            assert not isinstance(widget, Input)
            assert isinstance(widget, Label)
            return widget.render()

    text = _run_and_get(scenario)
    assert "example.com" in str(text)


def test_clearing_stated_suites_to_zero_is_refused():
    """Unticking every suite of a repository that *stated* its suites is the
    add flow's own guard, and it still applies here: the user has narrowed to
    nothing, which mirrors nothing."""
    src = _source()

    async def scenario():
        app = _host_app(EditSourceScreen(src))
        async with app.run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            await _pick(pilot, app, "suites", [])
            app.screen.query_one("#btn-save-source", Button).press()
            await _settle(pilot)
        return app.result

    result = _run_and_get(scenario)
    assert result is None


def test_dist_upgrade_and_gpg_key_url_survive_an_edit():
    """`RepositoryDraft.to_source()` has no field for either — both must be
    carried across by the screen itself, or editing silently drops them."""
    src = _source(dist_upgrade=True, gpg_key_url="https://example.com/key.gpg")

    async def scenario():
        app = _host_app(EditSourceScreen(src))
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
        app = _host_app(EditSourceScreen(src))
        async with app.run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#include-sources-switch", Switch).value = True
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
        app = _host_app(EditSourceScreen(src))
        async with app.run_test(size=(100, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#btn-save-source", Button).press()
            await _settle(pilot)
        return app.result

    result = _run_and_get(scenario)
    assert result is not None, "an all-suites repository must be saveable"
    assert result.suites == []          # still "all", not materialized
    assert result.identity == src.identity
