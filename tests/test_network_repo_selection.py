"""Network editor: repository selection is its own step/checklist, ahead of
the per-repository suite/component/arch pickers.

Before this, every repository's dimension pickers rendered pre-ticked with
nothing to indicate you could exclude a whole repository short of unticking
everything it exposed — a network mirroring three repositories forced the
user to hunt through all three to drop one. A repository checklist first
means excluding one is a single click.
"""
import asyncio

from textual.app import App, ComposeResult
from textual.widgets import Button, SelectionList

from mmw.core import config
from mmw.tui.network_screens import (
    AddNetworkScreen,
    EditNetworkScreen,
    _StepNetworkMirrors,
    _StepNetworkRepos,
)


def _mirror(name, source, arch="arm64", suite="noble", component="main"):
    return config.MirrorConfig(
        name=name, provider="apt", source=source, source_url=source,
        arch=arch, suite=suite, component=component,
    )


def _mirrors():
    return [
        _mirror("a", "https://a.example.com/repo"),
        _mirror("b", "https://b.example.com/repo"),
    ]


async def _settle(pilot, tries: int = 25):
    for _ in range(tries):
        await pilot.pause()


def _run(scenario):
    asyncio.run(scenario())


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


def test_repos_step_starts_with_nothing_checked():
    async def scenario():
        app = _host_app(AddNetworkScreen(_mirrors()))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            from textual.widgets import Input
            app.screen.query_one("#inp-name", Input).value = "net"
            await _settle(pilot)
            app.screen.query_one("#btn-next", Button).press()
            await _settle(pilot)
            repo_list = app.screen.query_one("#repo-list", SelectionList)
            assert repo_list.selected == []

    _run(scenario)


def test_next_with_no_repo_checked_does_not_advance():
    async def scenario():
        app = _host_app(AddNetworkScreen(_mirrors()))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            from textual.widgets import Input
            app.screen.query_one("#inp-name", Input).value = "net"
            await _settle(pilot)
            app.screen.query_one("#btn-next", Button).press()   # -> repositories
            await _settle(pilot)
            app.screen.query_one("#btn-next", Button).press()   # nothing checked
            await _settle(pilot)
            # Still on the repositories step.
            assert app.screen.query_one("#repo-list", SelectionList) is not None

    _run(scenario)


def test_only_checked_repositories_show_dimension_pickers():
    async def scenario():
        app = _host_app(AddNetworkScreen(_mirrors()))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            from textual.widgets import Input
            app.screen.query_one("#inp-name", Input).value = "net"
            await _settle(pilot)
            app.screen.query_one("#btn-next", Button).press()   # -> repositories
            await _settle(pilot)
            repo_list = app.screen.query_one("#repo-list", SelectionList)
            repo_list.select("https://a.example.com/repo")
            await _settle(pilot)
            app.screen.query_one("#btn-next", Button).press()   # -> dimensions
            await _settle(pilot)

            step = app.screen.query_one(_StepNetworkMirrors)
            assert len(step._axes) == 1
            assert step._axes[0].source == "https://a.example.com/repo"
            selection = step.get_selection()
            assert [s.source for s in selection] == ["https://a.example.com/repo"]

    _run(scenario)


def test_going_back_to_repositories_preserves_the_checked_set():
    async def scenario():
        app = _host_app(AddNetworkScreen(_mirrors()))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            from textual.widgets import Input
            app.screen.query_one("#inp-name", Input).value = "net"
            await _settle(pilot)
            app.screen.query_one("#btn-next", Button).press()   # -> repositories
            await _settle(pilot)
            app.screen.query_one("#repo-list", SelectionList).select("https://b.example.com/repo")
            await _settle(pilot)
            app.screen.query_one("#btn-next", Button).press()   # -> dimensions
            await _settle(pilot)
            app.screen.query_one("#btn-back", Button).press()   # -> repositories
            await _settle(pilot)

            repo_list = app.screen.query_one("#repo-list", SelectionList)
            assert repo_list.selected == ["https://b.example.com/repo"]

    _run(scenario)


def test_repos_step_widget_reports_axes_and_selection():
    """Direct unit check of the step widget, independent of the wizard shell."""
    step = _StepNetworkRepos(_mirrors())
    axes = step.get_axes()
    assert {a.source for a in axes} == {"https://a.example.com/repo", "https://b.example.com/repo"}


def test_edit_screen_defaults_to_all_checked_when_select_is_empty():
    """No `select` at all means every mirrored repository (see mmw.toml docs) —
    the checklist must reflect that as everything ticked, not nothing."""
    net = config.NetworkConfig(name="lab", root=None, select=[])
    screen = EditNetworkScreen(net, _mirrors())
    assert screen._checked_sources == {"https://a.example.com/repo", "https://b.example.com/repo"}


def test_edit_screen_only_checks_repositories_named_in_select():
    net = config.NetworkConfig(
        name="lab", root=None,
        select=[config.NetworkSelect(source="https://a.example.com/repo")],
    )
    screen = EditNetworkScreen(net, _mirrors())
    assert screen._checked_sources == {"https://a.example.com/repo"}


def test_unchecking_a_repo_in_edit_hides_its_dimensions_but_save_still_omits_it():
    net = config.NetworkConfig(
        name="lab", root=None,
        select=[
            config.NetworkSelect(source="https://a.example.com/repo"),
            config.NetworkSelect(source="https://b.example.com/repo"),
        ],
    )

    async def scenario():
        app = _host_app(EditNetworkScreen(net, _mirrors()))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            checklist = app.screen.query_one("#repo-checklist", SelectionList)
            assert set(checklist.selected) == {
                "https://a.example.com/repo", "https://b.example.com/repo",
            }
            checklist.deselect("https://b.example.com/repo")
            await _settle(pilot)

            screen = app.screen
            b_axis = next(a for a in screen._axes if a.source == "https://b.example.com/repo")
            assert screen.query_one("#" + b_axis.block_id()).display is False

            app.screen.query_one("#btn-save", Button).press()
            await _settle(pilot)
        return app.result

    result = asyncio.run(scenario())
    assert result is not None
    selects = result[0]
    assert [s.source for s in selects] == ["https://a.example.com/repo"]
