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
            unit = config.MirrorConfig(name="u-noble-main-amd64", provider="apt",
                                       source="https://archive.ubuntu.com/ubuntu",
                                       source_url="https://archive.ubuntu.com/ubuntu",
                                       suite="noble", component="main", arch="amd64")
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
