"""The rebuilt Networks section."""
import asyncio

from textual.widgets import Button, Input, ListView, SelectionList

from mmw.core import config
from mmw.tui import networks as N
from mmw.tui.app import MmwApp
from mmw.tui.network_screens import AddNetworkPage, EditNetworkPage
from mmw.tui.ui.dialog import ConfirmDialog

SRC = "https://deb.example.com/debian"


def _mirror(name="debian-bookworm-main-amd64"):
    return config.MirrorConfig(name=name, provider="apt", source=SRC, source_url=SRC,
                               arch="amd64", suite="bookworm", component="main")


def _cfg(tmp_path, *names, root=None):
    return config.Config(
        settings=config.Settings(mirror_dir=tmp_path / "pool"),
        sources=[config.SourceConfig(url=SRC, suites=["bookworm"], components=["main"],
                                     arches=["amd64"])],
        networks=[config.NetworkConfig(name=n, root=root, select=[config.NetworkSelect(source=SRC)])
                  for n in names],
    )


async def _settle(pilot, n=4):
    for _ in range(n):
        await pilot.pause()


async def _done(app, pilot):
    await _settle(pilot)
    await app.workers.wait_for_complete()
    await _settle(pilot)


def _run(tmp_path, check, cfg=None, monkeypatch=None, mirrors=True):
    cfg = cfg or _cfg(tmp_path, "lab", "dgx")
    config.save(cfg, tmp_path)
    if monkeypatch is not None:
        monkeypatch.setattr(N, "available", lambda c: [_mirror()] if mirrors else [])

    async def go():
        app = MmwApp(cfg, tmp_path, initial_section="networks")
        notes = []
        app.notify = lambda m, **_: notes.append(m)
        app.notes = notes
        async with app.run_test(size=(120, 40)) as pilot:
            await _done(app, pilot)
            await check(app, pilot)
    asyncio.run(go())


def _names(app):
    return [str(i.query_one("Label").render()).strip()
            for i in app.screen.query_one("#net-list", ListView).children]


def test_lists_networks_and_the_list_has_focus(tmp_path, monkeypatch):
    async def check(app, pilot):
        assert _names(app) == ["dgx", "lab"]
        assert app.focused is app.screen.query_one("#net-list")
    _run(tmp_path, check, monkeypatch=monkeypatch)


def test_add_refuses_clearly_when_nothing_is_synced(tmp_path, monkeypatch):
    async def check(app, pilot):
        await pilot.press("a")
        await _done(app, pilot)
        assert not isinstance(app.screen, AddNetworkPage)
        assert any("sync at least one repository first" in n for n in app.notes)
    _run(tmp_path, check, monkeypatch=monkeypatch, mirrors=False)


def test_add_through_the_wizard_saves(tmp_path, monkeypatch):
    async def check(app, pilot):
        await pilot.press("a")
        await _settle(pilot, 8)          # not _done: the add flow waits on this page
        page = app.screen
        assert isinstance(page, AddNetworkPage)
        page.query_one("#inp-net-name", Input).value = "edge"
        for _ in range(4):
            if page.step == 1:
                page.query_one("#repo-checklist", SelectionList).select(SRC)
                await _settle(pilot)
            page.query_one("#btn-wizard-next", Button).press()
            await _settle(pilot)
            await pilot.pause(0.05)
        await _done(app, pilot)
        assert "edge" in [n.name for n in config.load(tmp_path).networks]
        assert "edge" in _names(app)
    _run(tmp_path, check, monkeypatch=monkeypatch)


def test_edit_rename_moves_state_and_the_list_follows(tmp_path, monkeypatch):
    moved = []
    monkeypatch.setattr(N, "rename_state", lambda d, old, new: moved.append((old, new)) or [])

    async def check(app, pilot):
        lst = app.screen.query_one("#net-list", ListView)
        lst.index = _names(app).index("lab")
        await _settle(pilot)
        await pilot.press("e")
        await _settle(pilot, 8)          # not _done: the edit flow waits on this page
        page = app.screen
        assert isinstance(page, EditNetworkPage)
        page.query_one("#inp-net-name", Input).value = "lab2"
        page.query_one("#btn-save", Button).press()
        await _done(app, pilot)
        assert sorted(n.name for n in config.load(tmp_path).networks) == ["dgx", "lab2"]
        assert moved == [("lab", "lab2")]
        assert "lab2" in _names(app)
    _run(tmp_path, check, monkeypatch=monkeypatch)


def test_remove_clears_the_ledger_and_declining_keeps_the_published_files(tmp_path, monkeypatch):
    root = tmp_path / "srv" / "lab"
    root.mkdir(parents=True)
    cleared = []
    monkeypatch.setattr(N, "clear_ledger", lambda d, name: cleared.append(name))

    async def check(app, pilot):
        lst = app.screen.query_one("#net-list", ListView)
        lst.index = _names(app).index("lab")
        await _settle(pilot)
        await pilot.press("d")
        await _settle(pilot)
        assert isinstance(app.screen, ConfirmDialog)
        app.screen.query_one("#confirm").press()             # remove the network
        await _settle(pilot, 6)
        assert isinstance(app.screen, ConfirmDialog)         # "also delete published files?"
        await pilot.press("escape")                          # no
        await _done(app, pilot)
        assert [n.name for n in config.load(tmp_path).networks] == []
        assert cleared == ["lab"]
        assert root.exists()
    _run(tmp_path, check, cfg=_cfg(tmp_path, "lab", root=tmp_path / "srv" / "lab"),
         monkeypatch=monkeypatch)


import pytest  # noqa: E402


@pytest.mark.parametrize("key,verb", [("p", "publish"), ("r", "dry_run")])
def test_publish_keys_start_a_transfer_for_the_highlighted_network(tmp_path, monkeypatch,
                                                                     key, verb):
    """`b` (bundle) asks from/to first: tests/test_snapshot_tui.py."""
    from pickers import confirm_picker
    calls = []

    async def check(app, pilot):
        async def record(v, net, choice=None):
            calls.append((v, net.name))
        app.start_transfer = record
        lst = app.screen.query_one("#net-list", ListView)
        lst.index = _names(app).index("lab")
        await _settle(pilot)
        await pilot.press(key)
        if key == "p":
            await confirm_picker(app, pilot, "PublishPickPage")
        await _done(app, pilot)
        assert calls == [(verb, "lab")]
    _run(tmp_path, check, cfg=_cfg(tmp_path, "lab", "dgx", root=tmp_path / "pub"),
         monkeypatch=monkeypatch)


def test_c_runs_the_certs_flow_for_the_highlighted_network(tmp_path, monkeypatch):
    from mmw.tui import publish_flows
    calls = []

    async def record(self, net):
        calls.append(net.name)
    monkeypatch.setattr(publish_flows.PublishFlows, "certs", record)

    async def check(app, pilot):
        lst = app.screen.query_one("#net-list", ListView)
        lst.index = _names(app).index("lab")
        await _settle(pilot)
        await pilot.press("c")
        await _done(app, pilot)
        assert calls == ["lab"]
    _run(tmp_path, check, monkeypatch=monkeypatch)


def test_verbs_with_no_network_say_so(tmp_path, monkeypatch):
    async def check(app, pilot):
        await pilot.press("p")
        await _done(app, pilot)
        assert any("Select a network first" in n for n in app.notes)
    _run(tmp_path, check, cfg=_cfg(tmp_path), monkeypatch=monkeypatch)


def test_a_pool_error_during_a_verb_is_reported_not_fatal(tmp_path, monkeypatch):
    from mmw.tui import publish_flows

    def locked(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(publish_flows, "network_mirrors", locked)

    async def check(app, pilot):
        lst = app.screen.query_one("#net-list", ListView)
        lst.index = _names(app).index("lab")
        await _settle(pilot)
        from pickers import press_publish
        await press_publish(app, pilot)
        await _done(app, pilot)
        assert app.is_running
        assert any("database is locked" in n for n in app.notes)
    _run(tmp_path, check, cfg=_cfg(tmp_path, "lab", "dgx", root=tmp_path / "pub"),
         monkeypatch=monkeypatch)
