"""Folder repositories in the TUI: Add, Edit, signing keys, the sources file."""
import asyncio
import shutil

import pytest
from textual.widgets import Input, ListView, Select, TextArea

from mmw.core import config
from mmw.core.signing import Keyring
from mmw.tui.provider_ui import add_entries
from pickers import choose_add_entry
from test_add_source import _Harness
from waiting import until

pytestmark = pytest.mark.skipif(shutil.which("gpg") is None, reason="needs gpg")


class _CfgHarness(_Harness):
    """The harness, with the config a real app carries (mirror_dir, sources)."""

    def __init__(self, page_factory, cfg):
        super().__init__(page_factory)
        self.cfg = cfg


def _cfg(tmp_path, sources=()):
    return config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"), sources=list(sources))


def _src(tmp_path, **kw):
    return config.LocalAptSource(provider="apt-local", url="https://tools.lab/apt",
                                 folder=str(tmp_path / "debs"), key="tools", **kw)


def test_add_entry_offers_a_local_folder():
    entry = next(e for e in add_entries() if e.key == "apt-local")
    assert entry.label == "Local folder"


def test_add_local_folder_from_the_wizard(tmp_path):
    from mmw.tui.add_source import AddSourcePage

    async def scenario():
        app = _CfgHarness(lambda: AddSourcePage(), _cfg(tmp_path))
        async with app.run_test(size=(110, 36)) as pilot:
            await choose_add_entry(app, pilot, "apt-local")
            page = app.screen
            await until(pilot, lambda: page.query("#vs-folder"))
            page.query_one("#vs-url", Input).value = "https://tools.lab/apt"
            page.query_one("#vs-folder", Input).value = str(tmp_path / "debs")
            await pilot.pause()
            page.query_one("#btn-wizard-next").press()
            await until(pilot, lambda: app.result != "unset")
        (src,) = app.result
        assert isinstance(src, config.LocalAptSource) and src.url == "https://tools.lab/apt"
        assert src.key == src.name
        assert [k.name for k in Keyring(tmp_path / "m").list()] == [src.name]
    asyncio.run(scenario())


def test_edit_page_round_trips_unchanged(tmp_path):
    from mmw.tui.local_source import EditLocalPage
    Keyring(tmp_path / "m").generate("tools")
    src = _src(tmp_path, structured=True, suite="jammy")

    async def scenario():
        app = _CfgHarness(lambda: EditLocalPage(src, existing_names=set(), existing_urls=set()),
                          _cfg(tmp_path, [src]))
        async with app.run_test(size=(110, 36)) as pilot:
            await until(pilot, lambda: app.screen.query("#vs-key"))
            assert app.screen.query_one("#vs-key", Select).value == "tools"
            app.screen.query_one("#btn-save").press()
            await until(pilot, lambda: app.result != "unset")
        assert config.source_to_dict(app.result) == config.source_to_dict(src)
    asyncio.run(scenario())


def test_signing_keys_page_generate_and_refused_delete(tmp_path):
    from mmw.tui.signing_keys import SigningKeysPage
    Keyring(tmp_path / "m").generate("tools")

    async def scenario():
        app = _CfgHarness(SigningKeysPage, _cfg(tmp_path, [_src(tmp_path)]))
        async with app.run_test(size=(110, 36)) as pilot:
            page = app.screen
            await until(pilot, lambda: len(page.query_one("#keys-list", ListView).children) == 1)
            page.query_one("#key-name", Input).value = "spare"
            page.query_one("#btn-key-generate").press()
            await until(pilot, lambda: "Generated spare" in str(page.query_one("#page-status").render()))
            assert len(page.query_one("#keys-list", ListView).children) == 2
            page.query_one("#keys-list", ListView).index = 1          # "tools" sorts after "spare"
            page.query_one("#btn-key-delete").press()
            await until(pilot, lambda: "signs" in str(page.query_one("#page-status").render()))
            await until(pilot, lambda: not getattr(page, "_flow_busy", False))
            page.query_one("#keys-list", ListView).index = 0
            page.query_one("#btn-key-export").press()
            await until(pilot, lambda: "BEGIN PGP PUBLIC KEY" in page.query_one("#key-armor", TextArea).text)
        assert sorted(k.name for k in Keyring(tmp_path / "m").list()) == ["spare", "tools"]
    asyncio.run(scenario())


def test_sources_file_page_switches_format(tmp_path):
    from mmw.tui.sources_file import SourcesFilePage
    src = _src(tmp_path)

    async def scenario():
        app = _CfgHarness(lambda: SourcesFilePage(src), _cfg(tmp_path, [src]))
        async with app.run_test(size=(110, 36)) as pilot:
            page = app.screen
            await until(pilot, lambda: page.query("#sources-text"))
            assert "Types: deb" in page.query_one("#sources-text", TextArea).text
            page.query_one("#sources-format", Select).value = "list"
            await until(pilot, lambda: "deb [signed-by=" in page.query_one("#sources-text", TextArea).text)
    asyncio.run(scenario())


def test_settings_opens_the_signing_keys_page(tmp_path):
    from mmw.tui.sections.settings import SettingsPage

    async def scenario():
        app = _CfgHarness(SettingsPage, _cfg(tmp_path))
        async with app.run_test(size=(110, 40)) as pilot:
            await until(pilot, lambda: app.screen.query("#settings-signing-keys"))
            app.screen.query_one("#settings-signing-keys").press()
            await until(pilot, lambda: type(app.screen).__name__ == "SigningKeysPage")
    asyncio.run(scenario())
