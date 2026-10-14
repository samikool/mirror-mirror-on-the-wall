"""Where a network's gallery container listens (nginx proxies to it)."""
import pytest
from pydantic import ValidationError

from mmw.core import config


def test_default_and_round_trip(tmp_path):
    net = config.NetworkConfig(name="lab")
    assert net.gallery_port == 8765
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"),
                        networks=[net, config.NetworkConfig(name="edge", gallery_port=9000)])
    config.save(cfg, tmp_path)
    text = (tmp_path / "mmw.toml").read_text()
    assert text.count("gallery_port") == 1 and "gallery_port = 9000" in text
    assert [n.gallery_port for n in config.load(tmp_path).networks] == [8765, 9000]


@pytest.mark.parametrize("bad", [0, 70000])
def test_out_of_range(bad):
    with pytest.raises(ValidationError):
        config.NetworkConfig(name="lab", gallery_port=bad)


def test_the_network_form_edits_it():
    import asyncio
    from mmw.tui.network_screens import EditNetworkPage
    from mmw.tui.ui.base import ThemedApp
    from textual.widgets import Input
    from waiting import until

    got = []

    class _Host(ThemedApp):
        def on_mount(self):
            unit = config.MirrorConfig(name="vscode-server-linux-x64-stable", provider="vscode",
                                       source=config.MARKETPLACE_URL, source_url=config.MARKETPLACE_URL,
                                       dims={"kind": "server", "platform": "linux-x64", "quality": "stable"})
            self.push_screen(EditNetworkPage(config.NetworkConfig(name="lab"), [unit]), got.append)

    async def go():
        app = _Host()
        async with app.run_test(size=(100, 40)) as pilot:
            await until(pilot, lambda: app.screen.query("#inp-gallery-port"))
            app.screen.query_one("#inp-gallery-port", Input).value = "9001"
            app.screen.query_one("#btn-save").press()
            await until(pilot, lambda: got)
    asyncio.run(go())
    assert got[0].gallery_port == 9001


@pytest.mark.parametrize("text,want", [
    ("8765", 8765), ("1", 1), ("65535", 65535),
    ("0", None), ("65536", None), ("", None), ("abc", None), ("-1", None),
    ("²", None),                    # isdigit() but not int()-able — crashed the form
    ("٣٠٠٠", None),                 # Arabic-Indic digits: int() takes them; a port field shouldn't
])
def test_the_port_field_parses_ascii_ports_only(text, want):
    from mmw.tui.network_screens import parse_port
    assert parse_port(text) == want


def test_gallery_port_visibility_tracks_selected_provider_and_preserves_hidden_value():
    import asyncio
    from textual.widgets import Input, SelectionList
    from mmw.providers import get_provider
    from mmw.tui.network_screens import EditNetworkPage, NetworkForm
    from test_add_source import _Harness
    from waiting import until
    apt = config.SourceConfig(url='https://apt.example/repo', suites=['stable'],
                              components=['main'], arches=['amd64'])
    vs = config.VscodeSource(provider='vscode', kinds=['server'], platforms=['linux-x64'])
    units = apt.to_mirrors() + get_provider('vscode').expand(vs)
    net = config.NetworkConfig(name='lab', gallery_port=9876,
                               select=[config.NetworkSelect(source=apt.identity)])
    async def scenario():
        app = _Harness(lambda: EditNetworkPage(net, units))
        async with app.run_test(size=(100, 40)) as pilot:
            await until(pilot, lambda: app.screen.query(NetworkForm))
            form = app.screen.query_one(NetworkForm)
            field = form.query_one('#field-inp-gallery-port')
            assert not field.display
            form.query_one('#inp-gallery-port', Input).value = 'bad'
            result, error, _ = form.result()
            assert error is None
            assert result.gallery_port == 9876
            checklist = form.query_one('#repo-checklist', SelectionList)
            checklist.select(vs.identity)
            await until(pilot, lambda: field.display)
            assert form.result()[0] is None
            form.query_one('#inp-gallery-port', Input).value = '9001'
            checklist.deselect(vs.identity)
            await until(pilot, lambda: not field.display)
            checklist.select(vs.identity)
            await until(pilot, lambda: field.display)
            assert form.result()[0].gallery_port == 9001
    asyncio.run(scenario())


def test_add_network_hidden_port_uses_default_and_visible_port_is_validated():
    import asyncio
    from textual.widgets import Input, SelectionList
    from mmw.providers import get_provider
    from mmw.tui.network_screens import AddNetworkPage
    from test_add_source import _Harness
    from waiting import settle
    apt = config.SourceConfig(url='https://apt.example/repo', suites=['stable'],
                              components=['main'], arches=['amd64'])
    vs = config.VscodeSource(provider='vscode', kinds=['server'], platforms=['linux-x64'])
    units = apt.to_mirrors() + get_provider('vscode').expand(vs)
    async def scenario():
        app = _Harness(lambda: AddNetworkPage(units))
        async with app.run_test() as pilot:
            await settle(pilot)
            form = app.screen.form
            form.query_one('#inp-net-name', Input).value = 'lab'
            form.query_one('#repo-checklist', SelectionList).select(apt.identity)
            await pilot.pause()
            form.query_one('#inp-gallery-port', Input).value = 'bad'
            net, error, _ = form.result()
            assert error is None and net.gallery_port == 8765
            form.query_one('#repo-checklist', SelectionList).select(vs.identity)
            await pilot.pause()
            assert form.result()[0] is None
            form.query_one('#inp-gallery-port', Input).value = '9876'
            assert form.result()[0].gallery_port == 9876
    asyncio.run(scenario())
