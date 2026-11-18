"""Add a past point from the TUI: Repositories → t → a date → Look → Add point,
which runs on the Sync page and records the point."""
import asyncio

import pytest
from textual.widgets import Input, ListView, Static

from mmw.core import config
from mmw.core import past_points as pp
from mmw.core.pool import Pool

from test_past_points_record import lab  # noqa: F401 — the fixture
from waiting import settle, shows, until


@pytest.fixture
def setup(lab, monkeypatch):  # noqa: F811
    config.save(lab.cfg(), lab.tmp)
    monkeypatch.setattr(pp.Archive, "base", lambda self, name, stamp: lab.up.url)
    lab.older(("hello", "2.9-1"))
    return lab


def _past_dates(lb):
    with Pool(lb.mirror_dir, read_only=True) as pool:
        return sorted(p.date[:8] for t in pool.network_tracks("lab") if not t.repo.startswith("network:")
                      for p in pool.network_points("lab", t.id))


def _found(app):
    return lambda: str(app.screen.query_one("#past-found", Static).render())


@pytest.mark.parametrize("with_network", [True, False])
def test_add_a_past_point_from_repositories(setup, with_network):
    from mmw.tui.app import MmwApp
    from mmw.tui.past_points import PastPointPage
    from mmw.tui.ui.widgets import Button

    cfg = setup.cfg() if with_network else setup.cfg().model_copy(update={"networks": []})

    async def scenario():
        app = MmwApp(cfg, setup.tmp, initial_section="repositories")
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            await pilot.press("t")
            await until(pilot, lambda: isinstance(app.screen, PastPointPage))
            page = app.screen
            assert page.repo == setup.repo
            page.query_one("#past-when", Input).value = "2026-10-15"
            page.query_one("#btn-past-look", Button).press()
            await shows(pilot, _found(app), "Found: ", "as of 2026-10-15 23:59:59 UTC")
            assert not page.query_one("#btn-past-run", Button).disabled
            page.query_one("#btn-past-run", Button).press()
            await until(pilot, lambda: app.active_sync is not None and app.active_sync._past is not None)
            assert app.current_mode == "sync"
            sync = app.screen
            await shows(pilot, lambda: str(sync.query_one("#page-status").render()),
                        "Added: ", "jammy 2022-04-01")
            assert not app.active_sync._past_locked
    asyncio.run(scenario())
    with Pool(setup.mirror_dir) as pool:
        assert any(p.date.startswith("20220401") for p in pool.repository_points(setup.repo))


def test_editing_the_date_needs_another_look(setup):
    from mmw.tui.app import MmwApp
    from mmw.tui.ui.widgets import Button

    async def scenario():
        app = MmwApp(setup.cfg(), setup.tmp, initial_section="repositories",
                     start_past=("2026-10-15", None, setup.repo))
        async with app.run_test(size=(120, 40)) as pilot:
            await shows(pilot, _found(app), "Found: ")          # opened from the CLI: already looking
            page = app.screen
            page.query_one("#past-when", Input).value = "2026-10-16"
            await until(pilot, lambda: page.query_one("#btn-past-run", Button).disabled)
            assert _found(app)() == ""
    asyncio.run(scenario())


def test_a_repository_with_no_archive_says_why(setup, tmp_path):
    from mmw.tui.app import MmwApp
    other = config.SourceConfig(url="https://download.example.com/linux/repo", suites=["bookworm"],
                                components=["main"], arches=["amd64"])
    cfg = setup.cfg().model_copy(update={"sources": [setup.src, other]})

    async def scenario():
        app = MmwApp(cfg, setup.tmp, initial_section="repositories")
        notes = []
        app.notify = lambda message, **kw: notes.append(message)
        async with app.run_test(size=(120, 40)) as pilot:
            await settle(pilot)
            page = app.screen
            lst = page.query_one("#repo-list", ListView)
            lst.index = next(i for i, r in enumerate(page._rows)
                             if r.kind == "source" and r.source.url == other.url)
            await pilot.press("t")
            await until(pilot, lambda: notes)
            assert "snapshot.debian.org" in notes[-1] and "snapshot.ubuntu.com" in notes[-1]
            assert app.screen is page
    asyncio.run(scenario())


def test_look_failure_is_said_on_the_page(setup):
    from mmw.tui.app import MmwApp

    async def scenario():
        app = MmwApp(setup.cfg(), setup.tmp, initial_section="repositories",
                     start_past=("2022-04-01", None, setup.repo))
        async with app.run_test(size=(120, 40)) as pilot:
            await shows(pilot, _found(app), "the archive starts 2023-03-01")
    asyncio.run(scenario())
