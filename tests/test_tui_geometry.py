"""Every screen, at every realistic terminal size, is actually usable.

`test_tui_layout.py` asserts widgets are not off-screen. That is necessary and
insufficient: it passed for months while `EditNetworkScreen`'s `#axis-scroll`
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

from textual.app import App, ComposeResult

from mmw.core import config

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
    class _App(App):
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
    from mmw.tui.network_screens import EditNetworkScreen

    net = config.NetworkConfig(name="lab", select=[])
    _assert_usable(
        lambda: EditNetworkScreen(net, _mirrors()),
        # Not #repo-checklist: height:auto over two repositories is
        # legitimately two rows.
        ["#axis-scroll", "#edit-container"],
        "EditNetworkScreen",
    )


def test_edit_network_stays_usable_with_many_repositories():
    """Four repositories is an ordinary config and must not starve the region
    further than two did."""
    from mmw.tui.network_screens import EditNetworkScreen

    net = config.NetworkConfig(name="lab", select=[])
    _assert_usable(
        lambda: EditNetworkScreen(net, _mirrors(4)),
        ["#axis-scroll", "#edit-container"],
        "EditNetworkScreen(4 repos)",
    )


def test_add_network_steps_are_usable():
    from mmw.tui.network_screens import AddNetworkScreen

    _assert_usable(
        lambda: AddNetworkScreen(_mirrors(), set()),
        ["#step-container"],
        "AddNetworkScreen",
    )


def test_edit_source_screen_is_usable():
    from mmw.tui.source_screens import EditSourceScreen

    src = config.SourceConfig(url="https://deb.example.com/debian",
                              suites=["bookworm", "trixie"],
                              components=["main", "contrib"], arches=["amd64", "arm64"])
    _assert_usable(
        lambda: EditSourceScreen(src),
        ["#edit-source-container"],
        "EditSourceScreen",
    )


def test_pick_modal_list_is_usable():
    """archive.ubuntu.com publishes 55 suites; the list must not be a sliver."""
    from mmw.tui.pick import PickModal

    _assert_usable(
        lambda: PickModal("Suites", "pick some", [f"suite-{i}" for i in range(55)], []),
        ["#pick-list"],
        "PickModal(55)",
    )


def test_key_modal_is_usable():
    from mmw.tui.key_modal import KeyModal

    _assert_usable(lambda: KeyModal("debian"), ["#key-area"], "KeyModal")


def test_path_pick_tree_is_usable(tmp_path):
    """The tree is the widget — a path picker whose tree is a sliver is a
    text box with extra steps."""
    from mmw.tui.path_pick import PathPickModal

    (tmp_path / "keys").mkdir()
    _assert_usable(
        lambda: PathPickModal("Pick a directory", "somewhere", str(tmp_path)),
        ["#path-tree"],
        "PathPickModal",
    )


def test_bulk_review_pane_is_usable():
    """A DGX tree names a dozen repositories; the review pane is how they get
    triaged, so its list must not collapse."""
    from mmw.core.draft import RepositoryDraft
    from mmw.tui.bulk_review import BulkReviewScreen

    drafts = [
        RepositoryDraft(url=f"https://repo{i}.example.com/ubuntu", provider="apt")
        for i in range(6)
    ]
    _assert_usable(lambda: BulkReviewScreen(drafts), ["#repo-list"], "BulkReviewScreen")


def test_dry_run_result_modal_is_usable():
    from mmw.core.publisher.results import MirrorPublishResult, NetworkPublishResult
    from mmw.tui.publish_modals import DryRunResultModal

    result = NetworkPublishResult(
        network_name="lab",
        mirrors=[MirrorPublishResult(mirror_name=f"m-{i}", linked=i) for i in range(8)],
    )
    _assert_usable(lambda: DryRunResultModal("lab", result), ["#dry-run-table"],
                   "DryRunResultModal")


def test_certs_modal_is_usable():
    from mmw.tui.publish_modals import CertsModal

    _assert_usable(
        lambda: CertsModal("lab", [f"host{i}.example.com" for i in range(10)]),
        ["ScrollableContainer"],
        "CertsModal",
    )


def test_transfer_progress_modal_log_is_usable():
    """The log is where a publish or bundle reports what it is doing; a
    one-row log is indistinguishable from a hung transfer."""
    from mmw.tui.publish_modals import PublishProgressModal

    # Three rows at 80x24, not four. This one is allowed a lower floor because
    # it degrades *proportionally* rather than collapsing — 18 rows at 80x40,
    # 3 in a 21-row modal on a 24-row terminal — which is the correct behaviour
    # for a `1fr` pane absorbing whatever the fixed furniture leaves. The
    # default floor stays at 4 so it still catches the real failures this file
    # was written for: #axis-scroll at 1 row and #key-area at 0.
    _assert_usable(lambda: PublishProgressModal("lab", 100), ["#pub-log"],
                   "PublishProgressModal", min_rows=3)


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


def test_manage_list_panel_is_usable_and_adapts(tmp_path):
    """The source list was a fixed 36 columns at every terminal width — cramped
    at 80 and still 36 at 160, while the detail pane beside it sat empty. It is
    now proportional with a floor and a ceiling, so assert both ends: never
    narrower than the floor, never wider than the detail pane it shares with.
    """
    from pathlib import Path

    from mmw.tui.manage import ManageApp

    def app():
        return ManageApp(_demo_config(tmp_path), Path("."), initial_tab="tab-manage")

    widths = {}
    for size in SIZES:
        got = _probe_app(app, ["#list-panel", "#manage-list"], size)
        w, h = got["#list-panel"]
        assert h >= MIN_USABLE_ROWS, f"manage list panel {h} rows at {size}"
        # 35/63 rather than the CSS 36/64: the reported size is the content
        # box and this panel carries a 1-column border-right.
        assert w >= 35, f"manage list panel narrowed to {w} at {size}; floor is 35"
        assert w <= 63, f"manage list panel grew to {w} at {size}; ceiling is 63"
        widths[size] = w

    # It must actually adapt, not merely stay within bounds at one size.
    assert widths[(120, 40)] > widths[(80, 24)], (
        f"list panel did not widen with the terminal: {widths}"
    )


def test_networks_action_bar_buttons_all_fit(tmp_path):
    """Every action button must fit the bar, at every size.

    The bar has `overflow-x: auto`, so an overflowing button is not an error —
    it just scrolls off the right edge, present but invisible at the default
    width. That is close to not existing: Certs was added precisely because a
    verb with no button is undiscoverable, and scrolling it out of sight gives
    back the same problem.

    The margin here is thin by construction — 52 columns of button in a 52
    column bar at 80x24 — so this is the test that catches a renamed label,
    not a reviewer.
    """
    from pathlib import Path

    from mmw.tui.manage import ManageApp

    def app():
        return ManageApp(_demo_config(tmp_path), Path("."), initial_tab="tab-networks")

    async def go(size):
        a = app()
        async with a.run_test(size=size) as pilot:
            for _ in range(20):
                await pilot.pause()
            bar = a.screen.query_one("#pub-action-bar")
            buttons = list(a.screen.query("#pub-action-bar Button"))
            assert buttons, "the action bar has no buttons"
            # `virtual_size`, NOT the sum of `size.width`. Summing the buttons
            # was the first version of this assertion and it passed against a
            # layout where Certs was visibly cut off: `size.width` excludes each
            # button's `margin-right`, so seven buttons hid seven columns from
            # the total. The container already knows how wide its content is —
            # ask it, rather than re-deriving a number that can omit a box
            # model detail.
            return bar.size.width, bar.virtual_size.width, len(buttons)

    for size in SIZES:
        visible, needed, n = asyncio.run(go(size))
        assert needed <= visible, (
            f"{n} action buttons need {needed} columns but the bar shows "
            f"{visible} at {size[0]}x{size[1]} — the last ones scroll out of "
            f"sight. Shorten a label, or give the bar more width."
        )


def test_manage_action_bar_buttons_all_fit(tmp_path):
    """The Manage tab's bar, for the same reason as the Networks one.

    Both were docked inside their detail panel and got only what the list
    panel left over. This one wanted 47 columns in 40 at 80x24, leaving
    `⚠ Re-init` scrolled out of sight — a destructive action you cannot see
    is a worse outcome than a cramped one.
    """
    from pathlib import Path

    from mmw.tui.manage import ManageApp

    async def go(size):
        a = ManageApp(_demo_config(tmp_path), Path("."), initial_tab="tab-manage")
        async with a.run_test(size=size) as pilot:
            for _ in range(20):
                await pilot.pause()
            bar = a.screen.query_one("#action-bar")
            return bar.size.width, bar.virtual_size.width

    for size in SIZES:
        visible, needed = asyncio.run(go(size))
        assert needed <= visible, (
            f"manage action buttons need {needed} columns but the bar shows "
            f"{visible} at {size[0]}x{size[1]} — the last ones scroll out of sight."
        )


def test_network_detail_mirror_list_is_not_starved(tmp_path):
    """NetworkDetail's mirror list renders whole rather than as a sliver.

    `height: 1fr` inside a container that already scrolls resolves against the
    *visible* height, so the fixed detail lines above it (Root, Prune,
    Dist-upgrade, Certs, Publishes, the stats line, the Mirrors heading) took
    every row: 1 row for 5 mirrors at 80x24. Third instance of that trap after
    `#axis-scroll` and `#key-area`.
    """
    from pathlib import Path

    from mmw.tui.manage import ManageApp
    from mmw.tui.publish_panel import NetworkDetail

    async def go(size):
        a = ManageApp(_demo_config(tmp_path), Path("."), initial_tab="tab-networks")
        async with a.run_test(size=size) as pilot:
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
