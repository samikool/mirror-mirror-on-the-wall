"""Custom packages in the TUI: a network's page, the clash dialog, quick add."""
import asyncio
import shutil

import pytest
from textual.widgets import Label, ListView, Select

from debs import make_deb
from mmw.core import config
from mmw.core.layers import layer_dir, read_decisions
from test_add_source import _Harness
from test_layers import lab  # noqa: F401 — the fixture
from waiting import until

pytestmark = pytest.mark.skipif(shutil.which("gpg") is None or shutil.which("gpgv") is None,
                                reason="needs gpg and gpgv")


class _App(_Harness):
    """The harness with the config a real app carries, and its one writer."""

    def __init__(self, factory, cfg):
        super().__init__(factory)
        self.cfg = cfg

    async def save_config(self, change):
        self.cfg = change(self.cfg)


def _page(lab, cfg):  # noqa: F811
    from mmw.tui.packages import NetworkPackagesPage
    return lambda: NetworkPackagesPage(cfg.networks[0])


def test_signing_choice_saves_and_unsigned_warns(lab):  # noqa: F811
    cfg = lab.cfg()

    async def scenario():
        app = _App(_page(lab, cfg), cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            page = app.screen
            await until(pilot, lambda: page.query("#pkg-signing"))
            page.query_one("#pkg-signing", Select).value = "unsigned"
            await until(pilot, lambda: "trusted=yes" in str(page.query_one("#pkg-warning", Label).render()))
            page.query_one("#pkg-signing", Select).value = "resign"
            page.query_one("#pkg-key", Select).value = "tools"
            await pilot.pause()
            page.query_one("#btn-pkg-save").press()
            await until(pilot, lambda: app.cfg.networks[0].layers)
        layer = app.cfg.networks[0].layer_for(lab.src.url)
        assert (layer.signing, layer.key) == ("resign", "tools")
    asyncio.run(scenario())


def test_adding_a_clashing_deb_asks_and_drop_is_remembered(lab):  # noqa: F811
    layer = config.NetworkLayer(source=lab.src.url, signing="unsigned")
    cfg = lab.cfg(layer)
    clash = make_deb(lab.tmp / "new", "hello", "2.9")

    async def scenario():
        app = _App(_page(lab, cfg), cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            page = app.screen
            await until(pilot, lambda: page.query("#pkg-debs"))
            page.start_flow(lambda: page.add_debs([clash], "jammy", "main"))
            await until(pilot, lambda: type(app.screen).__name__ == "ClashDialog")
            app.screen.query_one("#btn-clash-drop").press()
            await until(pilot, lambda: len(page.query_one("#pkg-debs", ListView).children) == 1)
    asyncio.run(scenario())
    folder = layer_dir(lab.mirror_dir, "lab", lab.src.name)
    assert (folder / "jammy/main/hello_2.9_amd64.deb").is_file()
    assert read_decisions(folder) == {"hello_2.9_amd64.deb": "drop"}


def test_quick_add_into_a_folder_repository(lab, tmp_path):  # noqa: F811
    from mmw.tui.packages import AddPackagesPage
    src = config.LocalAptSource(provider="apt-local", url="https://tools.lab/apt",
                                folder=str(tmp_path / "debs"), key="tools")
    base = lab.cfg()
    cfg = base.model_copy(update={"sources": [*base.sources, src]})
    deb = make_deb(tmp_path / "new", "a", "1")

    async def scenario():
        app = _App(lambda: AddPackagesPage(paths=[deb]), cfg)
        async with app.run_test(size=(120, 40)) as pilot:
            page = app.screen
            await until(pilot, lambda: page.query("#quick-targets"))
            targets = page.query_one("#quick-targets")
            targets.select(src.name)
            await pilot.pause()
            page.query_one("#btn-quick-add").press()
            await until(pilot, lambda: (tmp_path / "debs" / deb.name).is_file())
    asyncio.run(scenario())
