"""Network editor: repository selection is its own step/checklist, ahead of
the per-repository suite/component/arch pickers.

Before this, every repository's dimension pickers rendered pre-ticked with
nothing to indicate you could exclude a whole repository short of unticking
everything it exposed — a network mirroring three repositories forced the
user to hunt through all three to drop one. A repository checklist first
means excluding one is a single click.
"""
import asyncio
from pathlib import Path

from textual.app import App, ComposeResult
from textual.widgets import Button, Switch, SelectionList

from mmw.core import config
from mmw.tui.manage import ManageApp
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
    assert [s.source for s in result.select] == ["https://a.example.com/repo"]


def test_edit_screen_dismisses_a_network_config_with_name_unchanged():
    """EditNetworkScreen used to dismiss an 8-tuple of positional fields —
    five of them bools, four positionally adjacent — so every new field grew
    the tuple and every consumer had to be updated in lockstep, with nothing
    but ordering to catch a mistake. It now dismisses a typed
    `config.NetworkConfig`, matching `AddNetworkScreen`. The editor never
    renames a network, so `name` must survive the round trip unchanged."""
    net = config.NetworkConfig(
        name="lab", root=None,
        select=[config.NetworkSelect(source="https://a.example.com/repo")],
    )

    async def scenario():
        app = _host_app(EditNetworkScreen(net, _mirrors()))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#btn-save", Button).press()
            await _settle(pilot)
        return app.result

    result = asyncio.run(scenario())
    assert isinstance(result, config.NetworkConfig)
    assert result.name == "lab"


def test_nginx_switch_round_trips_through_save():
    """`nginx` used to have no UI control at all — EditNetworkScreen._save
    hardcoded it back from `self._network.nginx`, so a switch that toggled it
    would have had no effect. Set it, save, and check the dismissed
    NetworkConfig actually carries the new value."""
    net = config.NetworkConfig(
        name="lab", root=None,
        select=[config.NetworkSelect(source="https://a.example.com/repo")],
    )
    assert net.nginx is True   # the model's own default

    async def scenario():
        app = _host_app(EditNetworkScreen(net, _mirrors()))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            switch = app.screen.query_one("#nginx-switch", Switch)
            assert switch.value is True
            switch.value = False
            await _settle(pilot)
            app.screen.query_one("#btn-save", Button).press()
            await _settle(pilot)
        return app.result

    result = asyncio.run(scenario())
    assert result is not None
    assert result.nginx is False


def test_dry_run_and_bundle_bindings_are_scoped_to_the_networks_tab(tmp_path):
    """`r`/`b` (Dry Run / Bundle) must be refused on the Manage tab and allowed
    on the Networks tab, exactly like the existing `p`/`e` (Publish/Edit)
    bindings — a binding missing from `check_action`'s `publish_only` set
    fires on both tabs."""
    cfg_obj = config.Config(settings=config.Settings(mirror_dir=tmp_path / "mirror"))

    async def scenario():
        app = ManageApp(cfg_obj, tmp_path)
        async with app.run_test() as pilot:
            await _settle(pilot)
            assert app.check_action("dry_run_network", ()) is False
            assert app.check_action("bundle_network", ()) is False

            app.action_publish_tab()
            await _settle(pilot)
            assert app.check_action("dry_run_network", ()) is True
            assert app.check_action("bundle_network", ()) is True

    asyncio.run(scenario())


def test_certs_button_exists_and_is_wired(tmp_path):
    """Certs had a keybinding (`c`) but no button — the exact inverse of Dry
    Run/Bundle, which had buttons but no bindings. The button must exist on
    the Networks tab and clicking it must reach `action_certs` (which warns
    rather than crashing when no network is selected)."""
    cfg_obj = config.Config(settings=config.Settings(mirror_dir=tmp_path / "mirror"))

    async def scenario():
        app = ManageApp(cfg_obj, tmp_path, initial_tab="tab-networks")
        # The action bar scrolls (overflow-x: auto) and Certs is now its
        # sixth button — a default 80x24 terminal clips it off-screen, which
        # is a `pilot.click` OutOfBounds, not a real defect (test_tui_layout.py
        # covers the geometry itself). A wider terminal keeps it in view.
        async with app.run_test(size=(160, 40)) as pilot:
            await _settle(pilot)
            button = app.query_one("#btn-certs", Button)
            assert button is not None
            await pilot.click("#btn-certs")
            await _settle(pilot)

    asyncio.run(scenario())


def test_edit_persists_every_networkconfig_field():
    """The edit path copies fields from the dismissed NetworkConfig onto the
    live one by iterating the model, not a hand-written list.

    A literal list there would reintroduce the positional 8-tuple's drift
    hazard in a new shape: add a field to NetworkConfig, forget to add it to
    the copy list, and the editor silently stops persisting it. That is
    precisely how `bundle_compress` would have been missed. This test fails
    the moment the two fall out of step.
    """
    import inspect

    from mmw.tui import publish_panel

    src = inspect.getsource(publish_panel.NetworkPanel.edit_network)
    assert "config.NetworkConfig.model_fields" in src, (
        "edit_network must derive the copied field list from the model"
    )
    # Every field, the name included: renaming is an edit like any other
    # (its certs and bundle ledger move with it — see rename_network_state).
    assert 'field != "name"' not in src
    assert "rename_network_state" in src


def test_add_network_refuses_clearly_when_there_are_no_mirrors_yet():
    """Before the first sync there is nothing to publish, and saying so beats
    walking the user into an empty checklist.

    A source with `arches = []` expands to no mirrors offline — that is the
    "never invent an architecture" rule — and available_mirrors()'s config
    fallback therefore has nothing to offer either. The wizard's own guard
    would have refused with "select at least one repository", which is true
    but blames the user for an empty list they could not have filled.
    """
    from mmw.tui.publish_panel import NetworkPanel

    notes: list[tuple[str, str]] = []

    class _FakeApp:
        def notify(self, msg, severity="information"):
            notes.append((msg, severity))

    panel = NetworkPanel.__new__(NetworkPanel)
    panel._available = lambda: []
    panel._cfg = config.Config(
        settings=config.Settings(mirror_dir=Path("/tmp/none")), sources=[], networks=[],
    )
    object.__setattr__(panel, "_fake_app", _FakeApp())
    type(panel).app = property(lambda self: self._fake_app)
    try:
        asyncio.run(NetworkPanel.add_network.__wrapped__(panel))
    finally:
        del type(panel).app

    assert notes, "adding a network with no mirrors must say something"
    msg, severity = notes[0]
    assert "sync" in msg.lower()
    assert severity == "warning"


# --- every NetworkConfig field is editable: name and bundle_tls -------------

def _edit_network(net, actions, existing_names=None):
    from textual.widgets import Button
    holder = {}

    async def scenario():
        app = _host_app(EditNetworkScreen(net, _mirrors(), existing_names=existing_names))
        async with app.run_test(size=(120, 50)) as pilot:
            await _settle(pilot)
            actions(app.screen)
            await _settle(pilot)
            app.screen.query_one("#btn-save", Button).press()
            await _settle(pilot)
        holder["result"] = app.result

    _run(scenario)
    return holder["result"]


def test_network_can_be_renamed():
    from textual.widgets import Input
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"))
    result = _edit_network(net, lambda s: setattr(
        s.query_one("#inp-net-name", Input), "value", "oak"))
    assert result.name == "oak"


def test_rename_onto_another_network_is_refused():
    from textual.widgets import Input
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"))
    result = _edit_network(net, lambda s: setattr(
        s.query_one("#inp-net-name", Input), "value", "oak"), existing_names={"lab", "oak"})
    assert result is None


def test_rename_to_an_unsafe_name_is_refused():
    from textual.widgets import Input
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"))
    assert _edit_network(net, lambda s: setattr(
        s.query_one("#inp-net-name", Input), "value", "../x")) is None


def test_bundle_tls_switch_is_saved():
    from textual.widgets import Switch

    def act(screen):
        screen.query_one("#certs-switch", Switch).value = True
        screen.query_one("#bundle-tls-switch", Switch).value = True
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"))
    result = _edit_network(net, act)
    assert result.certs and result.bundle_tls


def test_bundle_tls_without_certs_is_refused():
    from textual.widgets import Switch

    def act(screen):
        screen.query_one("#certs-switch", Switch).value = False
        screen.query_one("#bundle-tls-switch", Switch).value = True
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"))
    assert _edit_network(net, act) is None


def test_rename_moves_certs_and_ledger(tmp_path):
    for kind in ("certs", "bundles"):
        (tmp_path / kind / "lab").mkdir(parents=True)
        (tmp_path / kind / "lab" / "marker").write_text(kind)
    assert config.rename_network_state(tmp_path, "lab", "oak") == []
    assert (tmp_path / "certs" / "oak" / "marker").read_text() == "certs"
    assert (tmp_path / "bundles" / "oak" / "marker").read_text() == "bundles"
    assert not (tmp_path / "certs" / "lab").exists()


def test_rename_never_overwrites_existing_state(tmp_path):
    for name in ("lab", "oak"):
        (tmp_path / "certs" / name).mkdir(parents=True)
        (tmp_path / "certs" / name / "mirror.key").write_text(name)
    problems = config.rename_network_state(tmp_path, "lab", "oak")
    assert problems and "already exists" in problems[0]
    assert (tmp_path / "certs" / "oak" / "mirror.key").read_text() == "oak"
    assert (tmp_path / "certs" / "lab" / "mirror.key").read_text() == "lab"
