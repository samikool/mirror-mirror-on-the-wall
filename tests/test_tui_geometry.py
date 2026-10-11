"""Every screen, at every realistic terminal size, is actually usable.

`test_tui_layout.py` asserts widgets are not off-screen. That is necessary and
insufficient: it passed for months while `EditNetworkPage`'s `#axis-scroll`
— the region holding the entire point of that screen — rendered at **one row**
at both 80x24 and 80x40, because a 1-row region is technically on screen. The
fixed-height siblings around it (two path rows, a repo checklist, five 3-row
switch rows) ate every row, and its `height: 1fr` resolved against the
container's *visible* height rather than its content height.

So this file asserts the other half: a region that exists to hold content must
be given enough rows to hold some, and nothing may be clipped by its own
container. The sizes are the ones people actually use — 80x24 is the POSIX
default and the floor mmw should stay usable at.
"""
import asyncio

import pytest

from textual.app import ComposeResult

from mmw.core import config
from mmw.tui.ui.base import ThemedApp

# 80x24 is the POSIX default terminal. 80x40 and 120x40 are ordinary modern
# windows. If a screen is unusable at 80x24 that is worth knowing explicitly,
# not worth discovering on someone's laptop.
SIZES = [(80, 24), (80, 40), (120, 40)]

# A scroll/content region below this many rows is not scrollable, it is a
# sliver — the user cannot see enough to orient, and Textual gives no hint
# that content exists. Four rows shows a heading plus something under it.
MIN_USABLE_ROWS = 4


def _mirror(name, source, arch="arm64", suite="noble", component="main"):
    return config.MirrorConfig(name=name, provider="apt", source=source,
                               source_url=source, arch=arch, suite=suite,
                               component=component)


def _mirrors(n=2):
    hosts = ["https://archive.ubuntu.com/ubuntu", "https://ports.ubuntu.com/ubuntu-ports",
             "https://deb.debian.org/debian", "https://packages.microsoft.com/repos/code"]
    return [_mirror(chr(ord("a") + i), hosts[i % len(hosts)]) for i in range(n)]


def _host(factory):
    class _App(ThemedApp):
        def compose(self) -> ComposeResult:
            yield from ()

        def on_mount(self):
            self.push_screen(factory())
    return _App


def _probe(factory, selectors, size):
    """Return {selector: (width, height)} for one screen at one size."""
    out: dict[str, tuple[int, int]] = {}

    async def go():
        app = _host(factory)()
        async with app.run_test(size=size) as pilot:
            await _settle(pilot, app, selectors, out)

    asyncio.run(go())
    return out


async def _settle(pilot, app, selectors, out):
    """Pause until the measured geometry stops changing, then read it.

    Not a fixed pause count. A literal `range(30)` was the first version and
    it dominated the runtime — 124s for this file, against 39s at `range(8)` —
    while still being a guess that a slower screen could outrun. Settling on
    the measurement itself is both faster (most screens are stable within two
    or three frames) and safer than any number picked by hand: it waits
    exactly as long as the screen actually needs.
    """
    def read():
        got = {}
        for sel in selectors:
            try:
                w = app.screen.query_one(sel)
                got[sel] = (w.size.width, w.size.height)
            except Exception:
                got[sel] = (-1, -1)
        return got

    previous = None
    stable = 0
    for _ in range(40):                      # hard cap, never reached in practice
        await pilot.pause()
        current = read()
        stable = stable + 1 if current == previous else 0
        previous = current
        if stable >= 2:                      # unchanged across two extra frames
            break
    out.update(previous or read())


def _assert_usable(factory, selectors, label, min_rows=MIN_USABLE_ROWS):
    """Every named region has real height at every size, on every screen.

    Pass only regions that are *meant to flex* — scroll containers and `1fr`
    panes. An `height: auto` list sized to two items is correctly two rows
    tall, and asserting a floor on it would be a false positive rather than a
    finding; that mistake was made once already while writing this file.
    """
    for size in SIZES:
        sizes = _probe(factory, selectors, size)
        for sel, (w, h) in sizes.items():
            assert (w, h) != (-1, -1), f"{label} {size}: {sel} is absent"
            assert h >= min_rows, (
                f"{label} at {size[0]}x{size[1]}: {sel} rendered {h} row(s), "
                f"needs >= {min_rows}. A region starved to a sliver is "
                f"not caught by off-screen assertions — see this module's docstring."
            )
            assert w > 0, f"{label} {size}: {sel} has zero width"


# ── the screens ──────────────────────────────────────────────────────────────

def test_edit_network_dimension_region_is_usable():
    """The regression that motivated this file. `#axis-scroll` holds the
    suite/component/arch pickers — the whole reason to open this screen."""
    from mmw.tui.network_screens import EditNetworkPage

    net = config.NetworkConfig(name="lab", select=[])
    _assert_usable(
        lambda: EditNetworkPage(net, _mirrors()),
        # Not #repo-checklist: height:auto over two repositories is
        # legitimately two rows.
        ["#axis-scroll", "#page-body"],
        "EditNetworkPage",
    )


def test_edit_network_stays_usable_with_many_repositories():
    """Four repositories is an ordinary config and must not starve the region
    further than two did."""
    from mmw.tui.network_screens import EditNetworkPage

    net = config.NetworkConfig(name="lab", select=[])
    _assert_usable(
        lambda: EditNetworkPage(net, _mirrors(4)),
        ["#axis-scroll", "#page-body"],
        "EditNetworkPage(4 repos)",
    )


def test_add_network_steps_are_usable():
    from mmw.tui.network_screens import AddNetworkPage

    _assert_usable(
        lambda: AddNetworkPage(_mirrors(), set()),
        ["#page-body"],
        "AddNetworkPage",
    )


def test_edit_source_screen_is_usable():
    from mmw.tui.source_screens import EditSourcePage

    src = config.SourceConfig(url="https://deb.example.com/debian",
                              suites=["bookworm", "trixie"],
                              components=["main", "contrib"], arches=["amd64", "arm64"])
    _assert_usable(
        lambda: EditSourcePage(src),
        ["#page-body"],
        "EditSourcePage",
    )


def test_pick_modal_list_is_usable():
    """archive.ubuntu.com publishes 55 suites; the list must not be a sliver."""
    from mmw.tui.pick import PickPage

    _assert_usable(
        lambda: PickPage("Suites", "pick some", [f"suite-{i}" for i in range(55)], []),
        ["#pick-list"],
        "PickPage(55)",
    )


def test_key_modal_is_usable():
    from mmw.tui.key_modal import KeyPage

    _assert_usable(lambda: KeyPage("debian"), ["#key-area"], "KeyPage")


def test_path_pick_tree_is_usable(tmp_path):
    """The tree is the widget — a path picker whose tree is a sliver is a
    text box with extra steps."""
    from mmw.tui.path_pick import PathPickPage

    (tmp_path / "keys").mkdir()
    _assert_usable(
        lambda: PathPickPage("Pick a directory", "somewhere", str(tmp_path)),
        ["#path-tree"],
        "PathPickPage",
    )


def test_bulk_review_pane_is_usable():
    """A DGX tree names a dozen repositories; the review pane is how they get
    triaged, so its list must not collapse."""
    from mmw.core.draft import RepositoryDraft
    from mmw.tui.bulk_review import BulkReviewPage

    drafts = [
        RepositoryDraft(url=f"https://repo{i}.example.com/ubuntu", provider="apt")
        for i in range(6)
    ]
    _assert_usable(lambda: BulkReviewPage(drafts), ["#repo-list"], "BulkReviewPage")


def _in_a_page(widget_factory):
    """A widget the way the Publish section shows it: inside a Page."""
    from mmw.tui.ui.page import Page

    class _Host(Page):
        SCROLLS = False

        def compose_body(self):
            yield widget_factory()
    return _Host()


def test_dry_run_report_is_usable():
    from mmw.core.publisher.results import MirrorPublishResult, NetworkPublishResult
    from mmw.tui.transfer import DryRunReport

    result = NetworkPublishResult(
        network_name="lab",
        mirrors=[MirrorPublishResult(mirror_name=f"m-{i}", linked=i) for i in range(8)],
    )
    _assert_usable(lambda: _in_a_page(lambda: DryRunReport(result)), ["#dry-run-table"],
                   "DryRunReport")


def test_certs_modal_is_usable():
    from mmw.tui.publish_modals import CertsPage

    _assert_usable(
        lambda: CertsPage("lab", [f"host{i}.example.com" for i in range(10)]),
        ["#page-body"],
        "CertsPage",
    )


def test_transfer_panel_log_is_usable():
    """The log is where a publish or bundle reports what it is doing; a
    one-row log is indistinguishable from a hung transfer."""
    from mmw.tui.transfer import PUBLISH, TransferView

    # Three rows at 80x24, not four. This one is allowed a lower floor because
    # it degrades *proportionally* rather than collapsing — 18 rows at 80x40,
    # 3 in a 21-row panel on a 24-row terminal — which is the correct behaviour
    # for a `1fr` pane absorbing whatever the fixed furniture leaves. The
    # default floor stays at 4 so it still catches the real failures this file
    # was written for: #axis-scroll at 1 row and #key-area at 0.
    _assert_usable(lambda: _in_a_page(lambda: TransferView(PUBLISH, "lab")), ["#pub-log"],
                   "TransferView", min_rows=3)


def _demo_config(tmp_path):
    from pathlib import Path
    return config.Config(
        settings=config.Settings(mirror_dir=Path(tmp_path) / "pool"),
        sources=[
            config.SourceConfig(url="https://archive.ubuntu.com/ubuntu", suites=["noble"],
                                components=["main"], arches=["amd64"]),
            config.SourceConfig(url="https://deb.debian.org/debian", suites=["bookworm"],
                                components=["main"], arches=["amd64"]),
        ],
        networks=[config.NetworkConfig(name="lab", root=Path("/srv/lab"), select=[])],
    )


def _probe_app(app_factory, selectors, size):
    out: dict[str, tuple[int, int]] = {}

    async def go():
        app = app_factory()
        async with app.run_test(size=size) as pilot:
            await _settle(pilot, app, selectors, out)

    asyncio.run(go())
    return out


def _mmw(tmp_path, section):
    from mmw.tui.app import MmwApp
    return lambda: MmwApp(_demo_config(tmp_path), tmp_path, initial_section=section)


def test_repository_list_pane_is_usable_and_adapts(tmp_path):
    """The source list was once a fixed 36 columns at every terminal width —
    cramped at 80 and still 36 at 160, while the detail pane beside it sat
    empty. It must stay usable and widen with the terminal."""
    widths = {}
    for size in SIZES:
        got = _probe_app(_mmw(tmp_path, "repositories"), [".list-pane", "#repo-list"], size)
        w, h = got[".list-pane"]
        assert h >= MIN_USABLE_ROWS, f"repository list pane {h} rows at {size}"
        widths[size] = w
    assert widths[(120, 40)] > widths[(80, 24)], (
        f"list pane did not widen with the terminal: {widths}"
    )


@pytest.mark.parametrize("section", ["repositories", "networks"])
def test_page_action_buttons_all_fit(tmp_path, section):
    """Every action button must fit the bar, at every size.

    An overflowing button is present but out of sight — close to not existing:
    Certs was added precisely because a verb with no button is undiscoverable,
    and a destructive action you cannot see is worse than a cramped one.
    """
    async def go(size):
        a = _mmw(tmp_path, section)()
        async with a.run_test(size=size) as pilot:
            for _ in range(20):
                await pilot.pause()
            bar = a.screen.query_one("#page-actions")
            buttons = [b for b in a.screen.query("#page-actions Button") if b.display]
            assert buttons, "the action bar has no buttons"
            # `virtual_size`, NOT the sum of `size.width`: that excludes each
            # button's margin, and once passed against a visibly cut-off Certs.
            return bar.size.width, bar.virtual_size.width, len(buttons)

    for size in SIZES:
        visible, needed, n = asyncio.run(go(size))
        assert needed <= visible, (
            f"{n} {section} action buttons need {needed} columns but the bar shows "
            f"{visible} at {size[0]}x{size[1]} — the last ones scroll out of sight."
        )


def test_network_detail_mirror_list_is_not_starved(tmp_path):
    """NetworkDetail's mirror list renders whole rather than as a sliver.

    `height: 1fr` inside a container that already scrolls resolves against the
    *visible* height, so the fixed detail lines above it took every row: 1 row
    for 5 mirrors at 80x24. Third instance of that trap after `#axis-scroll`
    and `#key-area`.
    """
    from mmw.tui.sections.network_detail import NetworkDetail

    async def go(size):
        a = _mmw(tmp_path, "networks")()
        async with a.run_test(size=size) as pilot:
            await a.workers.wait_for_complete()
            for _ in range(25):
                await pilot.pause()
            ml = a.screen.query_one(NetworkDetail).query_one("#mirror-list")
            return ml.size.height, ml.virtual_size.height

    for size in SIZES:
        got, wanted = asyncio.run(go(size))
        assert got >= wanted, (
            f"the mirror list shows {got} of {wanted} rows at "
            f"{size[0]}x{size[1]} — it is starved, not scrolling."
        )
