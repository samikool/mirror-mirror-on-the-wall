"""The rebuilt Repositories section."""
import asyncio

from textual.widgets import Button, Input, ListView

from mmw.core import config, pool as pool_mod
from mmw.tui import repositories as R
from mmw.tui.app import MmwApp
from mmw.tui.source_screens import EditSourcePage
from mmw.tui.ui.dialog import ConfirmDialog
from waiting import settle, until


def _cfg(tmp_path, n=3):
    hosts = ["https://deb.example.com/debian", "https://deb.example.com/debian-security",
             "https://ppa.example.org/ubuntu"]
    return config.Config(
        settings=config.Settings(mirror_dir=tmp_path / "pool"),
        sources=[config.SourceConfig(url=hosts[i], suites=["bookworm"], components=["main"],
                                     arches=["amd64"]) for i in range(n)],
    )


async def _settle(pilot, n=4):
    """Until no work is in flight — see tests/waiting.py. The count is ignored."""
    await settle(pilot)


def _run(tmp_path, check, cfg=None, size=(120, 40)):
    cfg = cfg or _cfg(tmp_path)
    config.save(cfg, tmp_path)

    async def go():
        app = MmwApp(cfg, tmp_path, initial_section="repositories")
        async with app.run_test(size=size) as pilot:
            await _settle(pilot)
            await app.workers.wait_for_complete()
            await _settle(pilot)
            await check(app, pilot)
    asyncio.run(go())


def _labels(app):
    return [str(item.query_one("Label").render())
            for item in app.screen.query_one("#repo-list", ListView).children]


def test_lists_repositories_grouped_by_host_one_row_each(tmp_path):
    async def check(app, pilot):
        hosts = ["deb.example.com", "ppa.example.org"]
        await until(pilot, lambda: [lbl.strip() for lbl in _labels(app) if "/" not in lbl]
                    == hosts)
        labels = _labels(app)
        assert [lbl.strip() for lbl in labels if "/" not in lbl] == \
            ["deb.example.com", "ppa.example.org"]
        assert all(item.region.height == 1 for item in app.screen.query_one("#repo-list").children)
    _run(tmp_path, check)


def test_space_ticks_and_status_counts(tmp_path):
    async def check(app, pilot):
        lst = app.screen.query_one("#repo-list", ListView)
        lst.focus()
        lst.index = 1                           # first repository under the first host
        await _settle(pilot)
        await pilot.press("space")
        await _settle(pilot)
        assert app.screen.model.selection_summary() == "1 source"
        assert "1 source selected" in str(app.screen.query_one("#page-status").render())
        assert "✓" in _labels(app)[1]
    _run(tmp_path, check)


def test_enter_expands_and_detail_follows_highlight(tmp_path):
    async def check(app, pilot):
        lst = app.screen.query_one("#repo-list", ListView)
        lst.focus()
        lst.index = 1
        await _settle(pilot)
        assert "deb.example.com/debian" in str(app.screen.query_one("#repo-detail").render())
        await pilot.press("enter")
        await _settle(pilot)
        assert len(lst.children) == 6           # 2 hosts + 3 sources + 1 mirror
    _run(tmp_path, check)


def test_filter_narrows_and_hidden_highlight_is_not_acted_on(tmp_path):
    async def check(app, pilot):
        lst = app.screen.query_one("#repo-list", ListView)
        lst.focus()
        lst.index = 1
        await _settle(pilot)
        await pilot.press("slash")
        await _settle(pilot)
        box = app.screen.query_one("#repo-filter", Input)
        assert app.focused is box
        box.value = "ppa"
        await _settle(pilot)
        assert [lbl for lbl in _labels(app) if "debian" in lbl] == []
        row = app.screen.highlighted()
        assert row is None or row.source.url.startswith("https://ppa")
        await pilot.press("escape")             # clears the filter, back to the list
        await _settle(pilot)
        assert box.value == "" and len(lst.children) == 5
    _run(tmp_path, check)


def test_markup_in_names_is_shown_literally(tmp_path):
    cfg = _cfg(tmp_path, n=1)
    cfg.sources[0].name = "weird[b]name"

    async def check(app, pilot):
        assert any("weird[b]name" in lbl for lbl in _labels(app))
    _run(tmp_path, check, cfg=cfg)


def test_the_list_has_focus_so_section_keys_work(tmp_path):
    async def check(app, pilot):
        assert app.focused is app.screen.query_one("#repo-list")
        await pilot.press("5")
        await _settle(pilot)
        assert app.current_mode == "settings"
    _run(tmp_path, check)


async def _focus_row(app, pilot, index):
    lst = app.screen.query_one("#repo-list", ListView)
    lst.focus()
    lst.index = index
    await _settle(pilot)


async def _done(app, pilot):
    await _settle(pilot)
    await app.workers.wait_for_complete()
    await _settle(pilot)


def test_edit_opens_the_page_and_saves_by_identity(tmp_path):
    async def check(app, pilot):
        await _focus_row(app, pilot, 1)
        await pilot.press("e")
        await _settle(pilot)
        page = app.screen
        assert isinstance(page, EditSourcePage)
        page.query_one("#edit-source-name", Input).value = "renamed"
        page.query_one("#btn-save-source", Button).press()
        await _done(app, pilot)
        assert app.screen.TITLE == "Sources"
        assert "renamed" in [s.name for s in config.load(tmp_path).sources]
        assert any("renamed" in lbl for lbl in _labels(app))
    _run(tmp_path, check)


def test_remove_one_of_several_ticked_keeps_the_others_ticked(tmp_path):
    async def check(app, pilot):
        for row in (1, 2, 4):
            await _focus_row(app, pilot, row)
            await pilot.press("space")
        await _settle(pilot)
        assert app.screen.model.selection_summary() == "3 sources"
        await pilot.press("space")                   # untick the third (still highlighted)
        await _settle(pilot)
        await pilot.press("d")
        await _settle(pilot)
        assert isinstance(app.screen, ConfirmDialog)
        app.screen.query_one("#confirm").press()
        await _done(app, pilot)
        assert [s.url for s in config.load(tmp_path).sources] == ["https://ppa.example.org/ubuntu"]
        assert app.screen.model.selection_summary() == ""
    _run(tmp_path, check)


def test_remove_with_nothing_ticked_removes_only_the_highlighted(tmp_path):
    async def check(app, pilot):
        await _focus_row(app, pilot, 2)
        await pilot.press("d")
        await _settle(pilot)
        app.screen.query_one("#confirm").press()
        await _done(app, pilot)
        assert [s.url for s in config.load(tmp_path).sources] == [
            "https://deb.example.com/debian", "https://ppa.example.org/ubuntu"]
    _run(tmp_path, check)


def test_cancelled_remove_writes_nothing(tmp_path):
    async def check(app, pilot):
        before = config.config_path(tmp_path).read_text()
        await _focus_row(app, pilot, 1)
        await pilot.press("d")
        await _settle(pilot)
        await pilot.press("escape")
        await _done(app, pilot)
        assert config.config_path(tmp_path).read_text() == before
    _run(tmp_path, check)


def test_edit_and_remove_with_nothing_highlighted_do_nothing(tmp_path):
    async def check(app, pilot):
        box = app.screen.query_one("#repo-filter", Input)
        box.value = "no-such-repository"
        await _settle(pilot)
        app.screen.query_one("#repo-list", ListView).focus()
        await pilot.press("e")
        await pilot.press("d")
        await _done(app, pilot)
        assert app.screen.TITLE == "Sources"
        assert len(config.load(tmp_path).sources) == 3
    _run(tmp_path, check)


def test_add_opens_the_add_flow_and_cancel_writes_nothing(tmp_path):
    from mmw.tui.add_source import AddSourcePage

    async def check(app, pilot):
        before = config.config_path(tmp_path).read_text()
        await pilot.press("a")
        await _settle(pilot)
        assert isinstance(app.screen, AddSourcePage)
        await pilot.press("escape")
        await _done(app, pilot)
        assert app.screen.TITLE == "Sources"
        assert config.config_path(tmp_path).read_text() == before
    _run(tmp_path, check)


def _status(app):
    return str(app.screen.query_one("#page-status").render())


def test_prune_refused_while_the_pool_is_busy(tmp_path, monkeypatch):
    def busy(*a, **k):
        raise pool_mod.PoolBusy("a sync is using the pool")
    monkeypatch.setattr(R, "prune_pool", busy)

    async def check(app, pilot):
        await pilot.press("p")
        await _settle(pilot)
        app.screen.query_one("#confirm").press()
        await _done(app, pilot)
        assert "sync is using the pool" in _status(app)
        assert app.screen.query_one("#page-status").has_class("-error")
    _run(tmp_path, check)


def test_wipe_refused_while_the_pool_is_busy_deletes_nothing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(R, "pool_busy", lambda d: True)
    monkeypatch.setattr(pool_mod, "wipe", lambda d: calls.append(d))

    async def check(app, pilot):
        (tmp_path / "pool" / ".pool").mkdir(parents=True, exist_ok=True)
        await pilot.press("ctrl+w")
        await _settle(pilot)
        app.screen.query_one("#confirm").press()
        await _done(app, pilot)
        assert calls == []
        assert (tmp_path / "pool" / ".pool").exists()
        assert "Stop the sync first" in _status(app)
    _run(tmp_path, check)


def test_prune_reports_what_it_freed(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "prune_pool", lambda cfg, progress=None: (12, 4096))

    async def check(app, pilot):
        await pilot.press("p")
        await _settle(pilot)
        app.screen.query_one("#confirm").press()
        await _done(app, pilot)
        assert "Removed 12" in _status(app)
    _run(tmp_path, check)


def test_declined_wipe_does_nothing(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(pool_mod, "wipe", lambda d: calls.append(d))

    async def check(app, pilot):
        await pilot.press("ctrl+w")
        await _settle(pilot)
        await pilot.press("escape")
        await _done(app, pilot)
        assert calls == [] and app.screen.TITLE == "Sources"
    _run(tmp_path, check)


def test_overlapping_list_rebuilds_do_not_collide(tmp_path):
    async def check(app, pilot):
        page = app.screen
        await asyncio.gather(page.refresh_rows(), page.refresh_rows(), page.refresh_rows())
        await _settle(pilot)
        assert len(page.query_one("#repo-list").children) == 5
    _run(tmp_path, check)


def test_a_second_action_is_refused_instead_of_cancelling_the_first(tmp_path, monkeypatch):
    import threading

    from mmw.tui.add_source import AddSourcePage

    # Held until the test has tried the second action — a timed sleep lost
    # the race under parallel load (the prune finished first, so `a` was
    # rightly allowed).
    running, release = threading.Event(), threading.Event()

    def held_prune(cfg, progress=None):
        running.set()
        release.wait(30)
        return (3, 1024)
    monkeypatch.setattr(R, "prune_pool", held_prune)

    async def check(app, pilot):
        notes = []
        real_notify = app.screen.notify
        app.screen.notify = lambda m, **kw: (notes.append(str(m)), real_notify(m, **kw))
        await pilot.press("p")
        await until(pilot, lambda: app.screen.query("#confirm"))
        app.screen.query_one("#confirm").press()
        await until(pilot, running.is_set)
        await pilot.press("a")                       # while the prune runs
        # Refused, said so — and no Add page (a negative waits for the refusal).
        await until(pilot, lambda: any("Still working" in n for n in notes))
        assert not isinstance(app.screen, AddSourcePage)
        release.set()
        await _done(app, pilot)
        await until(pilot, lambda: "Removed 3" in _status(app))
    _run(tmp_path, check)


def test_the_list_follows_a_pool_directory_change_in_settings(tmp_path):
    async def check(app, pilot):
        new_dir = tmp_path / "elsewhere"
        await app.save_settings(mirror_dir=new_dir)
        await pilot.press("5")
        await _settle(pilot)
        await pilot.press("1")
        await _done(app, pilot)
        assert app.screen.model.cfg.settings.mirror_dir == new_dir
    _run(tmp_path, check)


def test_the_section_is_called_sources():
    from mmw.tui.navigation import SECTIONS
    from mmw.tui.sections.repositories import RepositoriesPage
    first = SECTIONS[0]
    assert (first.mode, first.label, first.key) == ("repositories", "Sources", "1")
    assert RepositoriesPage.TITLE == "Sources"


def test_prune_shows_its_progress(tmp_path, monkeypatch):
    import threading
    running, release = threading.Event(), threading.Event()

    def reporting_prune(cfg, progress=None):
        progress(5000, 12000)
        running.set()
        release.wait(30)
        return (12000, 4096)
    monkeypatch.setattr(R, "prune_pool", reporting_prune)

    async def check(app, pilot):
        await pilot.press("p")
        await until(pilot, lambda: app.screen.query("#confirm"))
        app.screen.query_one("#confirm").press()
        await until(pilot, running.is_set)
        await until(pilot, lambda: "5,000 of 12,000" in _status(app))
        release.set()
        await _done(app, pilot)
        await until(pilot, lambda: "Removed 12,000" in _status(app))
    _run(tmp_path, check)
