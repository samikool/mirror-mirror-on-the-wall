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
            for _ in range(30):
                await pilot.pause()
            for sel in selectors:
                try:
                    w = app.screen.query_one(sel)
                    out[sel] = (w.size.width, w.size.height)
                except Exception:
                    out[sel] = (-1, -1)

    asyncio.run(go())
    return out


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
