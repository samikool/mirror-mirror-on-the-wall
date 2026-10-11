"""Layout regression guard: nothing off-screen, nothing overlapping.

Every one of these caught a real defect. The manage tab's whole action bar
(Remove / Prune Pool / Wipe Pool / Re-init) sat below the viewport at every
terminal size, and the docked Re-init button drew on top of Wipe Pool — both
invisible to the test suite until the geometry was asserted directly.

Widgets inside a scrolling container are exempt: overflowing there is the
point. Everything else must fit.
"""
import asyncio
from pathlib import Path

import pytest
from textual.widgets import Button

from mmw.tui.ui.base import ThemedApp
from mmw.core import config

CFG = """
[settings]
mirror_dir = "/tmp/mmw-layout-test"
[[sources]]
name = "ubuntu-archive"
provider = "apt"
url = "http://archive.ubuntu.com/ubuntu"
arches = ["amd64"]
suites = ["noble", "noble-updates"]
components = ["main", "universe"]
[[networks]]
name = "lab"
root = "/tmp/mmw-layout-test-lab"
"""

# 80x24 is the classic minimum; the rest are common real terminals.
# 80x24 is the supported floor — the classic terminal minimum, and the
# narrowest width the publish tab's list panel + action bar can honestly fit.
# Anything smaller is out of scope rather than silently asserted.
SIZES = [(80, 24), (100, 30), (120, 40), (200, 50)]


@pytest.fixture
def cfg(tmp_path: Path):
    (tmp_path / "mmw.toml").write_text(CFG)
    return config.load(tmp_path), tmp_path


def _cfg_with_network(tmp_path: Path) -> config.Config:
    """A loaded config carrying the `lab` network from `CFG`, cfg_dir == tmp_path."""
    (tmp_path / "mmw.toml").write_text(CFG)
    return config.load(tmp_path)


def _scrollable_on(widget, axis: str) -> bool:
    """True if some ancestor can actually scroll `axis` to bring widget into view.

    Checking `overflow: auto` alone is not enough and silently voids the whole
    audit: a Textual `Screen` carries `overflow_y: auto` by default, so every
    widget in the app has a "scrollable" ancestor. The ancestor must also
    *have somewhere to scroll* (max_scroll_* > 0) for overflow to be legitimate.
    """
    node = widget
    while node is not None:
        styles = getattr(node, "styles", None)
        if styles is not None:
            overflow = getattr(styles, f"overflow_{axis}", None)
            if overflow in ("auto", "scroll"):
                room = getattr(node, f"max_scroll_{axis}", 0) or 0
                if room > 0:
                    return True
        node = node.parent
    return False


def find_problems(screen, width: int, height: int) -> list[str]:
    problems: list[str] = []
    widgets = [w for w in screen.query("*") if w.display and w.visible]

    for w in widgets:
        r = w.region
        if r.width == 0 or r.height == 0:
            continue
        name = f"{type(w).__name__}#{w.id}" if w.id else type(w).__name__
        if r.bottom > height and not _scrollable_on(w, "y"):
            problems.append(f"off-screen below: {name} y={r.y}..{r.bottom} > {height}")
        if r.right > width and not _scrollable_on(w, "x"):
            problems.append(f"off-screen right: {name} x={r.x}..{r.right} > {width}")

    buttons = [b for b in widgets if isinstance(b, Button)]
    for i, a in enumerate(buttons):
        for b in buttons[i + 1:]:
            if a.parent is b.parent and a.region.overlaps(b.region):
                problems.append(f"overlap: {a.id or '?'} {a.region} <> {b.id or '?'} {b.region}")

    # A child must fit inside its parent's *content* box. This is the check that
    # catches the whole hardcoded-size class without anyone having to remember
    # the rule: in Textual a fixed `height: N` is the border box, so `height: 3`
    # plus a `border-top` leaves only 2 rows for a 3-row Button — the bottom
    # border is clipped and the label stops looking vertically centred.
    for w in widgets:
        try:
            content = w.content_region
        except Exception:
            continue
        for child in w.children:
            if not (child.display and child.visible) or child.region.height == 0:
                continue
            r = child.region
            if _scrollable_on(child, "y"):
                continue
            if r.bottom > content.bottom or r.y < content.y:
                problems.append(
                    f"clipped: {type(w).__name__}#{w.id or '-'} content={content} "
                    f"cannot fit {type(child).__name__}#{child.id or '-'} {r}"
                )
    return problems


def _check(screen, size):
    problems = find_problems(screen, *size)
    assert not problems, f"at {size[0]}x{size[1]}:\n  " + "\n  ".join(problems)


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize("section", ["repositories", "networks", "sync", "settings"])
def test_section_layout(cfg, size, section):
    cfg_obj, cfg_dir = cfg
    from mmw.tui.app import MmwApp
    config.save(cfg_obj, cfg_dir)

    async def scenario():
        app = MmwApp(cfg_obj, cfg_dir, initial_section=section)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await pilot.pause()
            _check(app.screen, size)

    asyncio.run(scenario())


def test_networks_section_has_a_bundle_button(tmp_path):
    from mmw.tui.app import MmwApp

    cfg_obj = _cfg_with_network(tmp_path)
    config.save(cfg_obj, tmp_path)

    async def scenario():
        app = MmwApp(cfg_obj, tmp_path, initial_section="networks")
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.current_mode == "networks"
            assert app.screen.query_one("#btn-net-bundle", Button)

    asyncio.run(scenario())


def test_every_network_row_is_one_screen_row(tmp_path):
    """Same rule the repository list has: border-top + margin-top makes one row three."""
    from mmw.tui.app import MmwApp

    cfg_obj = _cfg_with_network(tmp_path)
    config.save(cfg_obj, tmp_path)

    async def scenario():
        app = MmwApp(cfg_obj, tmp_path, initial_section="networks")
        async with app.run_test() as pilot:
            await pilot.pause()
            items = list(app.screen.query("#net-list ListItem"))
            assert items
            for item in items:
                assert item.size.height == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("section", ["repositories", "networks"])
def test_page_action_bar_is_on_screen(cfg, section):
    """The specific regression: the button row must be visible, not below the fold."""
    cfg_obj, cfg_dir = cfg
    from mmw.tui.app import MmwApp
    config.save(cfg_obj, cfg_dir)

    async def scenario():
        app = MmwApp(cfg_obj, cfg_dir, initial_section=section)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            bar = app.screen.query_one("#page-actions")
            assert bar.region.bottom <= 30, f"action bar at {bar.region} is below the viewport"
            for btn in bar.query(Button):
                assert btn.region.bottom <= 30, f"{btn.id} at {btn.region} is below the viewport"

    asyncio.run(scenario())


@pytest.mark.parametrize("size", SIZES)
def test_network_screens_layout(cfg, size):
    cfg_obj, _ = cfg
    from textual import work
    from textual.app import ComposeResult

    from mmw.tui.network_screens import EditNetworkPage

    class _Host(ThemedApp):
        def compose(self) -> ComposeResult:
            yield from ()

        def on_mount(self):
            self._go()

        @work
        async def _go(self):
            await self.push_screen_wait(
                EditNetworkPage(cfg_obj.networks[0], cfg_obj.mirrors)
            )

    async def scenario():
        app = _Host()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await pilot.pause()
            _check(app.screen, size)

    asyncio.run(scenario())


# ── modal dismissal ───────────────────────────────────────────────────────────

def _modal_host(factory):
    from textual import work
    from textual.app import ComposeResult

    class _Host(ThemedApp):
        def __init__(self):
            super().__init__()
            self.result = "unset"

        def compose(self) -> ComposeResult:
            yield from ()

        def on_mount(self):
            self._go()

        @work
        async def _go(self):
            self.result = await self.push_screen_wait(factory())

    return _Host()


def test_escape_cancels_confirm_modal():
    """Escape must decline, never take the destructive branch."""
    from mmw.tui.ui.dialog import ConfirmDialog

    async def scenario():
        app = _modal_host(lambda: ConfirmDialog("Delete", "Really?"))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("escape")
            for _ in range(20):
                if app.result != "unset":
                    break
                await pilot.pause()
        assert app.result is False

    asyncio.run(scenario())


@pytest.mark.parametrize("size", SIZES)
@pytest.mark.parametrize(
    "factory,label",
    [
        (lambda: __import__("mmw.tui.ui.dialog", fromlist=["x"]).ConfirmDialog(
            "Wipe Package Pool",
            "Delete /very/long/path/to/the/pool?\nAll pooled packages will be removed.",
        ), "ConfirmDialog"),
        (lambda: __import__("mmw.tui.publish_modals", fromlist=["x"]).BundleModeModal(
            "a-reasonably-long-network-name"), "BundleModeModal"),
    ],
)
def test_modal_layout(size, factory, label):
    """Modals carry fixed widths (60/70/72), so a narrow terminal is the case
    that breaks them."""
    from textual import work
    from textual.app import ComposeResult

    class _Host(ThemedApp):
        def compose(self) -> ComposeResult:
            yield from ()

        def on_mount(self):
            self._go()

        @work
        async def _go(self):
            await self.push_screen_wait(factory())

    async def scenario():
        app = _Host()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            await pilot.pause()
            _check(app.screen, size)

    asyncio.run(scenario())
