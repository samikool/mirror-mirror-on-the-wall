"""Every control on every page can be reached and seen, at the sizes people use.

The bug that started the overhaul: three switches on the add-network Confirm
step sat below the fold with no scrollbar. This drives focus to every focusable
widget in turn — Textual scrolls the focused widget into view — and checks it
is then fully on screen, fully inside the page body if it lives there, and not
under the action bar.
"""
import asyncio
from pathlib import Path

import pytest

from mmw.core import config
from mmw.tui.app import MmwApp
from mmw.tui.navigation import SECTION_MODES
from waiting import settle

SIZES = [(80, 24), (120, 40)]


def _cfg():
    return config.Config(settings=config.Settings(mirror_dir=Path("/srv/pool"),
                                                  max_connections=8))


async def _settle(pilot, n=3):
    """Until no work is in flight — see tests/waiting.py. The count is ignored."""
    await settle(pilot)


async def unreachable(app, pilot) -> list[str]:
    page = app.screen
    body = page.query_one("#page-body")
    actions = page.query_one("#page-actions")
    problems = []
    # Skip widgets that are hidden on purpose (e.g. a closed Select's overlay):
    # they are focusable in principle but not meant to be on screen.
    candidates = [w for w in page.query("*")
                  if w.focusable and all(a.display for a in w.ancestors_with_self)]
    for w in candidates:
        w.focus()
        await _settle(pilot)
        r = w.region
        name = f"{type(w).__name__}#{w.id}"
        if r.height == 0 or r.width == 0:
            problems.append(f"{name} has no visible area")
        elif not page.region.contains_region(r):
            problems.append(f"{name} {r} is off screen {page.region}")
        elif body in w.ancestors and not body.region.contains_region(r):
            problems.append(f"{name} {r} not scrolled into the body {body.region}")
        elif actions not in w.ancestors_with_self and r.overlaps(actions.region):
            problems.append(f"{name} {r} is under the action bar {actions.region}")
    return problems


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize("section", SECTION_MODES)
def test_every_control_is_reachable(tmp_path, section, size):
    async def go():
        app = MmwApp(_cfg(), tmp_path, initial_section=section)
        async with app.run_test(size=size) as pilot:
            await _settle(pilot)
            return await unreachable(app, pilot)
    assert asyncio.run(go()) == []


def _pushed_pages(tmp_path):
    from mmw.core.draft import SourceDraft
    from mmw.tui.add_source import AddSourcePage
    from mmw.tui.bulk_review import BulkReviewPage
    from mmw.tui.key_modal import KeyPage
    from mmw.tui.network_screens import AddNetworkPage, EditNetworkPage
    from mmw.tui.publish_modals import CertsPage
    from mmw.tui.path_pick import PathPickPage
    from mmw.tui.pick import PickPage
    from mmw.tui.source_screens import EditSourcePage

    src = config.SourceConfig(url="https://deb.example.com/debian", suites=["bookworm", "trixie"],
                              components=["main", "contrib"], arches=["amd64", "arm64"])
    return {
        "EditSourcePage": lambda: EditSourcePage(src),
        "KeyPage": lambda: KeyPage("debian", "debian-archive.gpg"),
        "PickPage": lambda: PickPage("Suites", "sub", [f"suite-{i}" for i in range(55)], []),
        "PathPickPage": lambda: PathPickPage("Pick", "sub", str(tmp_path)),
        "AddSourcePage": lambda: AddSourcePage(),
        "BulkReviewPage": lambda: BulkReviewPage(
            [SourceDraft(url=f"https://r{i}.example.com/u") for i in range(5)]),
        "EditNetworkPage": lambda: EditNetworkPage(
            config.NetworkConfig(name="lab", select=[]), _net_mirrors()),
        "AddNetworkPage": lambda: AddNetworkPage(_net_mirrors()),
        "CertsPage": lambda: CertsPage("lab", [f"host{i}.example.com" for i in range(30)]),
    }


def _net_mirrors():
    out = []
    for i, host in enumerate(["https://a.example.com/u", "https://b.example.com/d"]):
        for suite in ("noble", "jammy", "focal"):
            for arch in ("amd64", "arm64"):
                out.append(config.MirrorConfig(
                    name=f"r{i}-{suite}-main-{arch}", provider="apt", source=host,
                    source_url=host, arch=arch, suite=suite, component="main"))
    return out


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize("name", ["EditSourcePage", "KeyPage", "PickPage", "PathPickPage",
                                  "AddSourcePage", "BulkReviewPage",
                                  "EditNetworkPage", "AddNetworkPage", "CertsPage"])
def test_every_control_on_pushed_pages_is_reachable(tmp_path, name, size):
    async def go():
        app = MmwApp(_cfg(), tmp_path, initial_section="settings")
        async with app.run_test(size=size) as pilot:
            await _settle(pilot)
            app.push_screen(_pushed_pages(tmp_path)[name]())
            await _settle(pilot)
            return await unreachable(app, pilot)
    assert asyncio.run(go()) == []
