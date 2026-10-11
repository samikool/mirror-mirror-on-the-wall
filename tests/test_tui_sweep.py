"""Sweep every widget on every screen for content hidden by the layout.

`test_tui_geometry.py` names specific regions and asserts they are big
enough. That only protects regions someone thought to name — and this family
of bug has now been fixed five times, twice after being looked at directly
and missed. So this file names nothing: it mounts each screen, walks the whole
widget tree, and flags the two runtime signatures every one of those five
produced.

**Why not grep the CSS instead.** That was tried. There are ~67 fixed
`width:`/`height:` declarations in `src/mmw/tui/`, and almost all are correct
— `height: 1` on a one-line title, `height: 3` on an Input row, `width: 92`
on a modal root. The defect is never in the declaration; it is in what the
declaration does to a *sibling*. There is no CSS shape to match on, so this
matches on the outcome instead.

The two signatures, and the bugs that produced them:

1. **Hidden behind a horizontal scroll** — a scrollable container whose
   content does not fit, so the last items sit off the right edge. Both action
   bars did this: the Networks bar needed 59 columns in 52 and the Manage bar
   47 in 40, leaving `Certs` and `⚠ Re-init` present but invisible at 80
   columns.

2. **A starved scroll region** — a container that *can* scroll but is given so
   few rows that scrolling is not a usable substitute for space.
   `EditNetworkPage`'s `#axis-scroll` rendered at 1 row for 34 rows of
   content, `KeyPage`'s paste box at 0, and `NetworkDetail`'s mirror list at
   1 row for 5 mirrors.

**The measure is `max_scroll_x` / `max_scroll_y`, not `virtual_size` against
`size`.** A non-scrollable widget routinely reports `virtual_size` a couple of
columns wider than `size` — that is padding, or text that will be ellipsised,
and flagging it produced ~95% false positives when this sweep was first
written. Asking the container whether it actually has somewhere to scroll *to*
needs no tolerance constant and has no such noise.
"""
import asyncio
import tempfile
from pathlib import Path

from textual.app import ComposeResult

from mmw.core import config
from mmw.tui.ui.base import ThemedApp
from mmw.core.draft import RepositoryDraft
from mmw.core.publisher.results import MirrorPublishResult, NetworkPublishResult

SIZES = [(80, 24), (100, 32)]

# A scroll region below this many rows is not scrolling, it is a sliver: the
# user cannot see enough to know what they are scrolling through. Matches
# test_tui_geometry.MIN_USABLE_ROWS.
MIN_USABLE_ROWS = 4

# Textual's own chrome. These are excluded because they are not ours to fix
# and they report their padding as scrollable overflow — not because they are
# uninteresting. Listed explicitly rather than matched by module, so that a
# Textual rename shows up as new noise to investigate rather than silently
# widening the exclusion.
FRAMEWORK = {
    "Header", "HeaderIcon", "HeaderTitle", "HeaderClock", "HeaderClockSpace",
    "Footer", "FooterKey", "Tabs", "Tab", "ContentTab", "Underline", "Bar",
    "TabbedContent", "ContentSwitcher", "TabPane", "Tooltip", "Toast",
}

# Widgets that scroll their own content as their whole purpose. A DataTable
# wider than its frame, or a DirectoryTree deeper than its pane, is navigable
# by design and reports exactly the same signature as a clipped action bar.
SELF_SCROLLING = {"DataTable", "Tree", "DirectoryTree", "_FilteredTree", "RichLog", "TextArea"}


def _mirror(name, source, arch="arm64", suite="noble", component="main"):
    return config.MirrorConfig(name=name, provider="apt", source=source,
                               source_url=source, arch=arch, suite=suite,
                               component=component)


def _mirrors(n=3):
    hosts = ["https://archive.ubuntu.com/ubuntu", "https://ports.ubuntu.com/ubuntu-ports",
             "https://deb.debian.org/debian"]
    return [_mirror(chr(ord("a") + i), hosts[i % len(hosts)]) for i in range(n)]


def _demo_config():
    tmp = Path(tempfile.mkdtemp())
    return config.Config(
        settings=config.Settings(mirror_dir=tmp / "pool"),
        sources=[
            config.SourceConfig(url="https://archive.ubuntu.com/ubuntu",
                                suites=["noble", "noble-updates"],
                                components=["main", "universe"], arches=["amd64"]),
            config.SourceConfig(url="https://ports.ubuntu.com/ubuntu-ports",
                                suites=["noble"], components=["main"], arches=["arm64"]),
        ],
        networks=[
            config.NetworkConfig(name="lab", root=Path("/srv/lab"), select=[]),
            config.NetworkConfig(name="dgx", bundle_dir=Path("/media/usb"), select=[]),
        ],
    )


def _host(factory):
    class _App(ThemedApp):
        def compose(self) -> ComposeResult:
            yield from ()

        def on_mount(self):
            self.push_screen(factory())
    return _App


def _inspect(widget) -> str | None:
    """Return a complaint about this widget, or None if it is fine."""
    cls = widget.__class__.__name__
    if cls in FRAMEWORK or cls in SELF_SCROLLING:
        return None
    if not widget.display or not widget.size.width:
        return None
    # Only layout containers. A leaf control reports scroll about its own box,
    # not about anything it is hiding: a Textual `Button` is a 3-row bordered
    # box and reports `max_scroll_y == 2` whenever it renders as one row, and a
    # `Static` whose text outruns its width reports `max_scroll_x` for text
    # that `text-overflow: ellipsis` is already handling. Neither is a sibling
    # being starved, which is the entire family this sweep exists to catch —
    # all five instances of it were containers hiding their own children.
    if not any(c.display for c in widget.children):
        return None

    if widget.max_scroll_x > 0:
        return (f"content runs {widget.max_scroll_x} column(s) past the right edge "
                f"({widget.size.width} visible) — the last items are present but "
                f"off-screen")
    if widget.max_scroll_y > 0 and widget.size.height < MIN_USABLE_ROWS:
        return (f"scrolls vertically but is only {widget.size.height} row(s) tall "
                f"({widget.max_scroll_y} row(s) hidden) — starved, not scrolling")
    return None


def _sweep(app_factory, label, size) -> list[str]:
    problems: list[str] = []

    async def go():
        app = app_factory()
        async with app.run_test(size=size) as pilot:
            # Settle on a stable tree rather than a fixed pause count — see
            # test_tui_geometry._settle for why.
            previous, stable = None, 0
            for _ in range(40):
                await pilot.pause()
                current = [(w.size, w.virtual_size) for w in app.screen.query("*")]
                stable = stable + 1 if current == previous else 0
                previous = current
                if stable >= 2:
                    break
            for w in app.screen.query("*"):
                complaint = _inspect(w)
                if complaint:
                    ident = w.__class__.__name__ + (f"#{w.id}" if w.id else "")
                    problems.append(f"{label} at {size[0]}x{size[1]}: {ident} {complaint}")

    asyncio.run(go())
    return problems


def _screens():
    """Every screen that can be mounted without network or pool access.

    A running sync is deliberately absent: it starts real HTTP on mount, which
    is also why it is in neither of the other TUI test files.
    """
    from mmw.tui.bulk_review import BulkReviewPage
    from mmw.tui.key_modal import KeyPage
    from mmw.tui.modals import InfoModal
    from mmw.tui.ui.dialog import ConfirmDialog, HelpDialog
    from mmw.tui.network_screens import AddNetworkPage, EditNetworkPage
    from mmw.tui.path_pick import PathPickPage
    from mmw.tui.pick import PickPage
    from mmw.tui.publish_modals import (
        BundleModeModal, CertsPage, DryRunResultPage, PreparePublishModal,
        PublishProgressModal,
    )
    from mmw.tui.source_screens import EditSourcePage

    net = config.NetworkConfig(name="lab", select=[])
    src = config.SourceConfig(url="https://deb.example.com/debian",
                              suites=["bookworm", "trixie"],
                              components=["main", "contrib"], arches=["amd64", "arm64"])
    result = NetworkPublishResult(
        network_name="lab",
        mirrors=[MirrorPublishResult(mirror_name=f"ubuntu-archive-noble-main-{i}", linked=i)
                 for i in range(6)],
    )
    return {
        "EditNetworkPage": lambda: EditNetworkPage(net, _mirrors()),
        "AddNetworkPage": lambda: AddNetworkPage(_mirrors(), set()),
        "EditSourcePage": lambda: EditSourcePage(src),
        "KeyPage": lambda: KeyPage("debian", "debian-archive.gpg"),
        "PickPage": lambda: PickPage("Suites", "sub", [f"suite-{i}" for i in range(55)], []),
        "BulkReviewPage": lambda: BulkReviewPage(
            [RepositoryDraft(url=f"https://repo{i}.example.com/ubuntu") for i in range(5)]),
        "BundleModeModal": lambda: BundleModeModal("lab"),
        "CertsPage": lambda: CertsPage("lab", [f"host{i}.example.com" for i in range(6)]),
        "DryRunResultPage": lambda: DryRunResultPage("lab", result),
        "PublishProgressModal": lambda: PublishProgressModal("lab", 100),
        "PreparePublishModal": lambda: PreparePublishModal("lab"),
        "PathPickPage": lambda: PathPickPage("Pick a directory", "sub", tempfile.mkdtemp()),
        "InfoModal": lambda: InfoModal("Title", "A message body."),
        "ConfirmDialog": lambda: ConfirmDialog("Title", "Really do the thing?"),
        "HelpDialog": lambda: HelpDialog("Repositories", [(k, f"does {k}") for k in "asdpe"],
                                         "Notes about this page."),
    }


def test_no_screen_hides_content_behind_the_layout():
    """No modal or step may clip its own content at a realistic terminal size."""
    problems: list[str] = []
    for label, factory in _screens().items():
        for size in SIZES:
            problems += _sweep(_host(factory), label, size)
    assert not problems, "\n  " + "\n  ".join(problems)


def test_the_main_app_hides_nothing_in_any_section():
    """The highest-traffic screens, swept whole — both action bars were
    caught here rather than by any named assertion."""
    from mmw.tui.app import MmwApp

    cfg = _demo_config()
    problems: list[str] = []
    for section in ("repositories", "networks", "settings", "sync"):
        for size in SIZES:
            problems += _sweep(
                lambda s=section: MmwApp(cfg, Path(tempfile.mkdtemp()), initial_section=s),
                f"MmwApp[{section}]", size)
    assert not problems, "\n  " + "\n  ".join(problems)
