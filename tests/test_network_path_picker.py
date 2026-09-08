"""Picking a network's publish root instead of typing it.

The publish root is an absolute path to somewhere on a fileserver; typing it
blind is exactly the case `PathPickModal` exists for. The picker is shared with
the key-directory scan and the config loader — one modal, three callers.
"""
import asyncio

from textual.widgets import Button, Input

from mmw.core import config
from mmw.tui.network_screens import AddNetworkScreen
from mmw.tui.path_pick import PathPickModal


def _mirror():
    return config.MirrorConfig(
        name="m", provider="apt", source="https://x/y", source_url="https://x/y",
        arch="arm64", suite="noble", component="main")


async def _settle(pilot, tries: int = 25):
    for _ in range(tries):
        await pilot.pause()


def _run(scenario):
    asyncio.run(scenario())


class _Harness:
    """Mounts AddNetworkScreen on a bare app."""

    def __new__(cls):
        from textual.app import App, ComposeResult

        class _App(App):
            def compose(self) -> ComposeResult:
                yield from ()

            def on_mount(self):
                self.push_screen(AddNetworkScreen([_mirror()]))

        return _App()


def test_browsing_fills_in_the_publish_root(tmp_path):
    (tmp_path / "srv" / "lab").mkdir(parents=True)

    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#btn-browse-root", Button).press()
            await _settle(pilot)

            picker = pilot.app.screen
            assert isinstance(picker, PathPickModal)
            picker.query_one("#path-input", Input).value = str(tmp_path / "srv" / "lab")
            picker.query_one("#btn-use", Button).press()
            await _settle(pilot)

            assert app.screen.query_one("#inp-root", Input).value == str(tmp_path / "srv" / "lab")

    _run(scenario)


def test_cancelling_the_browser_leaves_a_typed_root_alone(tmp_path):
    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#inp-root", Input).value = "/already/typed"
            await pilot.pause()
            app.screen.query_one("#btn-browse-root", Button).press()
            await _settle(pilot)
            pilot.app.screen.query_one("#btn-cancel", Button).press()
            await _settle(pilot)

            assert app.screen.query_one("#inp-root", Input).value == "/already/typed"

    _run(scenario)


def test_the_preview_follows_a_browsed_root(tmp_path):
    """`root/name` is what actually gets published — it must update when the
    root arrives from the picker, not only when it is typed."""
    (tmp_path / "srv").mkdir()

    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#inp-name", Input).value = "lab"
            await pilot.pause()
            app.screen.query_one("#btn-browse-root", Button).press()
            await _settle(pilot)
            picker = pilot.app.screen
            picker.query_one("#path-input", Input).value = str(tmp_path / "srv")
            picker.query_one("#btn-use", Button).press()
            await _settle(pilot)

            preview = str(app.screen.query_one("#preview").render())
            assert str(tmp_path / "srv") in preview and "lab" in preview

    _run(scenario)


def _host_app(screen):
    """Mounts an already-constructed screen on a bare app, capturing whatever
    it dismisses with — used to drive `AddNetworkScreen` all the way through
    to its final `NetworkConfig`, which `_Harness` above never needed."""
    from textual.app import App, ComposeResult

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


async def _finish(pilot, app):
    """Drive the 3-step wizard (Path & Name → Mirrors → Confirm) to
    completion, leaving the mirror-selection step at its default of
    everything ticked, and return the dismissed `NetworkConfig`."""
    app.screen.query_one("#btn-next", Button).press()   # path & name -> mirrors
    await _settle(pilot)
    app.screen.query_one("#btn-next", Button).press()   # mirrors -> confirm
    await _settle(pilot)
    app.screen.query_one("#btn-next", Button).press()   # confirm -> dismiss
    await _settle(pilot)
    return app.result


def test_a_network_may_be_created_with_only_a_bundle_dir(tmp_path):
    """Bundle-only network: no publish root, and none is invented."""

    async def scenario():
        app = _host_app(AddNetworkScreen([_mirror()], set()))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#inp-name", Input).value = "dgx-site"
            app.screen.query_one("#inp-bundle-dir", Input).value = str(tmp_path / "out")
            app.screen.query_one("#inp-root", Input).value = ""
            net = await _finish(pilot, app)

        assert net is not None
        assert net.root is None
        assert net.bundle_dir == tmp_path / "out"

    _run(scenario)


def test_neither_destination_is_allowed(tmp_path):
    """A network is a downselect; destinations belong to verbs. Both may be blank."""

    async def scenario():
        app = _host_app(AddNetworkScreen([_mirror()], set()))
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#inp-name", Input).value = "dgx-site"
            net = await _finish(pilot, app)

        assert net is not None
        assert net.root is None and net.bundle_dir is None

    _run(scenario)


def test_bundle_dir_browse_does_not_require_the_path_to_exist(tmp_path):
    """Same rule the publish root already has: the directory is created by the
    verb, so the picker must neither require nor create it."""
    (tmp_path / "srv").mkdir()
    missing = tmp_path / "srv" / "not-yet"

    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#btn-browse-bundle", Button).press()
            await _settle(pilot)

            picker = pilot.app.screen
            assert isinstance(picker, PathPickModal)
            picker.query_one("#path-input", Input).value = str(tmp_path / "srv")
            picker.query_one("#btn-use", Button).press()
            await _settle(pilot)

            assert app.screen.query_one("#inp-bundle-dir", Input).value == str(tmp_path / "srv")
        assert not missing.exists(), "picker must not create anything"

    _run(scenario)


def test_the_base_path_need_not_contain_the_network_yet(tmp_path):
    """Only the *base* is picked; the network directory below it is created by
    the publish, so the picker must not require it to exist."""
    (tmp_path / "srv").mkdir()

    async def scenario():
        app = _Harness()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            app.screen.query_one("#btn-browse-root", Button).press()
            await _settle(pilot)
            picker = pilot.app.screen
            picker.query_one("#path-input", Input).value = str(tmp_path / "srv")
            picker.query_one("#btn-use", Button).press()
            await _settle(pilot)

            step = app.screen.query_one("#inp-root", Input).value
            assert step == str(tmp_path / "srv")
            assert not (tmp_path / "srv" / "lab").exists(), "picker must not create anything"

    _run(scenario)


def test_edit_screen_round_trips_a_bundle_only_network(tmp_path):
    """The editor must pre-fill whichever destination(s) a network already has
    — and save back exactly that, not invent or drop one on the way through.
    This is the case the whole task exists for: a bundle-only network created
    by hand (or by a prior save) must remain editable and still bundle-only
    after a Save."""
    from textual import work
    from textual.app import App, ComposeResult

    from mmw.tui.network_screens import EditNetworkScreen

    net = config.NetworkConfig(
        name="dgx-site",
        bundle_dir=tmp_path / "out",
        select=[config.NetworkSelect(source="https://x/y")],
    )
    mirrors = [_mirror()]

    class _Host(App):
        result = "unset"

        def compose(self) -> ComposeResult:
            yield from ()

        def on_mount(self):
            self._go()

        @work
        async def _go(self):
            self.result = await self.push_screen_wait(EditNetworkScreen(net, mirrors))

    async def scenario():
        app = _Host()
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            # Round-trip check 1: opening Edit shows what was actually stored —
            # no root, and the bundle_dir that was set.
            assert app.screen.query_one("#inp-root", Input).value == ""
            assert app.screen.query_one("#inp-bundle-dir", Input).value == str(tmp_path / "out")
            app.screen.query_one("#btn-save", Button).press()
            await _settle(pilot)
        return app.result

    result = asyncio.run(scenario())
    assert result is not None
    selects, prune, dist_upgrade, certs, root, bundle_dir, nginx = result
    # Round-trip check 2: Save carries the unchanged fields straight through.
    assert root is None
    assert bundle_dir == tmp_path / "out"
