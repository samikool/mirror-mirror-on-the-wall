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
from textual.widgets import Button, Checkbox, SelectionList

from mmw.core import config
from mmw.tui.network_screens import AddNetworkPage, EditNetworkPage, NetworkForm
from waiting import settle


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
    """Until no work is in flight — see tests/waiting.py. The count is ignored."""
    await settle(pilot)


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
        app = _host_app(AddNetworkPage(_mirrors()))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            from textual.widgets import Input
            app.screen.query_one("#inp-net-name", Input).value = "net"
            await _settle(pilot)
            app.screen.query_one("#btn-wizard-next", Button).press()
            await _settle(pilot)
            repo_list = app.screen.query_one("#repo-checklist", SelectionList)
            assert repo_list.selected == []

    _run(scenario)


def test_next_with_no_repo_checked_does_not_advance():
    async def scenario():
        app = _host_app(AddNetworkPage(_mirrors()))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            from textual.widgets import Input
            app.screen.query_one("#inp-net-name", Input).value = "net"
            await _settle(pilot)
            app.screen.query_one("#btn-wizard-next", Button).press()   # -> repositories
            await _settle(pilot)
            app.screen.query_one("#btn-wizard-next", Button).press()   # nothing checked
            await _settle(pilot)
            # Still on the repositories step.
            assert app.screen.query_one("#repo-checklist", SelectionList) is not None

    _run(scenario)


def test_only_checked_repositories_show_dimension_pickers():
    async def scenario():
        app = _host_app(AddNetworkPage(_mirrors()))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            from textual.widgets import Input
            app.screen.query_one("#inp-net-name", Input).value = "net"
            await _settle(pilot)
            app.screen.query_one("#btn-wizard-next", Button).press()   # -> repositories
            await _settle(pilot)
            repo_list = app.screen.query_one("#repo-checklist", SelectionList)
            repo_list.select("https://a.example.com/repo")
            await _settle(pilot)
            app.screen.query_one("#btn-wizard-next", Button).press()   # -> dimensions
            await _settle(pilot)

            form = app.screen.query_one(NetworkForm)
            assert [a.source for a in form.checked_axes()] == ["https://a.example.com/repo"]
            assert [s.source for s in form.selects()] == ["https://a.example.com/repo"]

    _run(scenario)


def test_going_back_to_repositories_preserves_the_checked_set():
    async def scenario():
        app = _host_app(AddNetworkPage(_mirrors()))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            from textual.widgets import Input
            app.screen.query_one("#inp-net-name", Input).value = "net"
            await _settle(pilot)
            app.screen.query_one("#btn-wizard-next", Button).press()   # -> repositories
            await _settle(pilot)
            app.screen.query_one("#repo-checklist", SelectionList).select("https://b.example.com/repo")
            await _settle(pilot)
            app.screen.query_one("#btn-wizard-next", Button).press()   # -> dimensions
            await _settle(pilot)
            app.screen.query_one("#btn-wizard-back", Button).press()   # -> repositories
            await _settle(pilot)

            repo_list = app.screen.query_one("#repo-checklist", SelectionList)
            assert repo_list.selected == ["https://b.example.com/repo"]

    _run(scenario)


def test_repos_step_widget_reports_axes_and_selection():
    """Direct unit check of the form, independent of the wizard shell."""
    axes = NetworkForm(_mirrors(), mode="add")._axes
    assert {a.source for a in axes} == {"https://a.example.com/repo", "https://b.example.com/repo"}


def test_edit_screen_defaults_to_all_checked_when_select_is_empty():
    """No `select` at all means every mirrored repository (see mmw.toml docs) —
    the checklist must reflect that as everything ticked, not nothing."""
    net = config.NetworkConfig(name="lab", root=None, select=[])
    screen = EditNetworkPage(net, _mirrors())
    assert screen._checked_sources == {"https://a.example.com/repo", "https://b.example.com/repo"}


def test_edit_screen_only_checks_repositories_named_in_select():
    net = config.NetworkConfig(
        name="lab", root=None,
        select=[config.NetworkSelect(source="https://a.example.com/repo")],
    )
    screen = EditNetworkPage(net, _mirrors())
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
        app = _host_app(EditNetworkPage(net, _mirrors()))
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
    """EditNetworkPage used to dismiss an 8-tuple of positional fields —
    five of them bools, four positionally adjacent — so every new field grew
    the tuple and every consumer had to be updated in lockstep, with nothing
    but ordering to catch a mistake. It now dismisses a typed
    `config.NetworkConfig`, matching `AddNetworkPage`. The editor never
    renames a network, so `name` must survive the round trip unchanged."""
    net = config.NetworkConfig(
        name="lab", root=None,
        select=[config.NetworkSelect(source="https://a.example.com/repo")],
    )

    async def scenario():
        app = _host_app(EditNetworkPage(net, _mirrors()))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#btn-save", Button).press()
            await _settle(pilot)
        return app.result

    result = asyncio.run(scenario())
    assert isinstance(result, config.NetworkConfig)
    assert result.name == "lab"


def test_nginx_switch_round_trips_through_save():
    """`nginx` used to have no UI control at all — EditNetworkPage._save
    hardcoded it back from `self._network.nginx`, so a switch that toggled it
    would have had no effect. Set it, save, and check the dismissed
    NetworkConfig actually carries the new value."""
    net = config.NetworkConfig(
        name="lab", root=None,
        select=[config.NetworkSelect(source="https://a.example.com/repo")],
    )
    assert net.nginx is True   # the model's own default

    async def scenario():
        app = _host_app(EditNetworkPage(net, _mirrors()))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            switch = app.screen.query_one("#nginx-switch", Checkbox)
            assert switch.value is True
            switch.value = False
            await _settle(pilot)
            app.screen.query_one("#btn-save", Button).press()
            await _settle(pilot)
        return app.result

    result = asyncio.run(scenario())
    assert result is not None
    assert result.nginx is False


def test_certs_button_exists_and_is_wired(tmp_path):
    """Certs once had a keybinding (`c`) but no button. The button must exist
    in the Networks section and pressing it must reach the certs verb (which
    says what is missing rather than crashing when no network is selected)."""
    from mmw.tui.app import MmwApp

    cfg_obj = config.Config(settings=config.Settings(mirror_dir=tmp_path / "mirror"))
    config.save(cfg_obj, tmp_path)
    notes = []

    async def scenario():
        app = MmwApp(cfg_obj, tmp_path, initial_section="networks")
        app.notify = lambda m, **_: notes.append(m)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#btn-net-certs", Button).press()
            await _settle(pilot)

    asyncio.run(scenario())
    assert any("network" in n.lower() for n in notes), notes


def test_edit_persists_every_networkconfig_field(tmp_path):
    """An edit replaces the whole NetworkConfig — no hand-written list of
    fields to copy, so a field added to the model (as `nginx` once
    was) can never be silently dropped by the editor. Every field set away
    from its default must survive the replace and a save/load round trip."""
    from mmw.tui import networks

    edited = config.NetworkConfig(
        name="renamed", root=tmp_path / "srv", bundle_dir=tmp_path / "usb",
        select=[config.NetworkSelect(source="https://deb.example.com/debian")],
        prune=False, dist_upgrade=True, certs=False,
        nginx=False, retention="30d", far_side="mmw",
    )
    defaults = config.NetworkConfig(name="x")
    unchanged = [f for f in config.NetworkConfig.model_fields
                 if getattr(edited, f) == getattr(defaults, f)]
    assert unchanged == [], f"set these fields in the test too: {unchanged}"

    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "pool"),
                        networks=[config.NetworkConfig(name="lab")])
    out = networks.replace_network(cfg, "lab", edited)
    config.save(out, tmp_path)
    assert config.load(tmp_path).networks == [edited]


# --- every NetworkConfig field is editable: name and certs -----------------

def _edit_network(net, actions, existing_names=None):
    from textual.widgets import Button
    holder = {}

    async def scenario():
        app = _host_app(EditNetworkPage(net, _mirrors(), existing_names=existing_names))
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


def test_certs_switch_is_saved_and_says_bundles_carry_the_key():
    from textual.widgets import Checkbox

    def act(screen):
        assert not screen.query("#bundle-tls-switch")      # folded into certs
        field = next(f for f in screen.query("ToggleField")
                     if f.query("#certs-switch"))
        assert "private key" in field.hint and "bundle" in field.hint.lower()
        assert screen.query_one("#certs-switch", Checkbox).value     # on by default
        screen.query_one("#certs-switch", Checkbox).value = False    # and can be turned off
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"))
    assert not _edit_network(net, act).certs


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


def test_edit_page_controls_are_one_row_and_a_bad_name_is_refused_in_place():
    from textual.widgets import Input

    from mmw.tui.ui.base import ThemedApp

    net = config.NetworkConfig(name="lab", root=None, select=[])
    got = []

    class _Host(ThemedApp):
        def on_mount(self):
            self.push_screen(EditNetworkPage(net, _mirrors()), got.append)

    async def scenario():
        app = _Host()
        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot)
            page = app.screen
            assert all(w.region.height == 1 for w in page.query("Input, Button, Checkbox")
                       if all(a.display for a in w.ancestors_with_self))
            page.query_one("#inp-net-name", Input).value = "a/b"
            page.query_one("#btn-save", Button).press()
            await _settle(pilot)
            assert "letters, digits" in str(page.query_one("#page-status").render())
            assert app.focused.id == "inp-net-name"
            assert got == []

    _run(scenario)


def test_add_page_refuses_a_bad_name_at_the_first_step():
    from textual.widgets import Input

    async def scenario():
        app = _host_app(AddNetworkPage(_mirrors()))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#inp-net-name", Input).value = "a/b"
            app.screen.query_one("#btn-wizard-next", Button).press()
            await _settle(pilot)
            assert app.screen.step == 0

    _run(scenario)
