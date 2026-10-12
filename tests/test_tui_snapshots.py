"""Approved renders of every screen, so a layout regression fails CI.

`test_tui_geometry.py` asserts a region is *big enough*; this asserts the
whole screen still *looks* the way it was approved looking. The two catch
different things — geometry caught `#axis-scroll` collapsing to one row, but
it would say nothing about a label that started wrapping, a colour that went
unreadable, or a button that drifted under a scrollbar.

Baselines live in `tests/__snapshots__/` and are committed. They were approved
by eye once, which is the only point in this process a human is strictly
required; after that, drift is a test failure rather than something someone
has to notice.

**When one of these fails**, run `uv run pytest --snapshot-update` ONLY after
looking at the diff report pytest prints the path to. A snapshot test that is
updated reflexively is worse than no snapshot test, because it launders a
regression into an approved baseline.

Sizes match `test_tui_geometry.py`: 80x24 is the POSIX default and the floor
mmw should stay usable at.
"""
from mmw.core import config
from waiting import settle, until

# One representative size per screen rather than the geometry file's three.
# Snapshots are for catching visual drift, and three baselines per screen
# would triple the review burden on every legitimate change for very little
# extra signal — geometry already covers the size sweep.
SNAP_SIZE = (100, 32)


def _mirror(name, source, arch="arm64", suite="noble", component="main"):
    return config.MirrorConfig(name=name, provider="apt", source=source,
                               source_url=source, arch=arch, suite=suite,
                               component=component)


def _mirrors(n=2):
    hosts = ["https://archive.ubuntu.com/ubuntu", "https://ports.ubuntu.com/ubuntu-ports",
             "https://deb.debian.org/debian", "https://packages.microsoft.com/repos/code"]
    return [_mirror(chr(ord("a") + i), hosts[i % len(hosts)]) for i in range(n)]


def _app(factory):
    """A host app that pushes one screen, for snap_compare to drive."""
    from textual.app import ComposeResult

    from mmw.tui.ui.base import ThemedApp

    # ThemedApp, not App: converted screens are Pages styled by mmw.tcss, and
    # an approved baseline must show them as users see them.
    class _Host(ThemedApp):
        def compose(self) -> ComposeResult:
            yield from ()

        def on_mount(self):
            self.push_screen(factory())

    return _Host()


# `snap_compare` wants a path or an App instance; these fixtures hand it the
# latter so each screen can be constructed with the data that makes it
# interesting rather than whatever a module-level app would have.

def test_snapshot_edit_network(snap_compare):
    from mmw.tui.network_screens import EditNetworkPage

    net = config.NetworkConfig(name="lab", select=[])
    assert snap_compare(_app(lambda: EditNetworkPage(net, _mirrors())),
                        terminal_size=SNAP_SIZE)


def test_snapshot_edit_source(snap_compare):
    from mmw.tui.source_screens import EditSourcePage

    src = config.SourceConfig(url="https://deb.example.com/debian",
                              suites=["bookworm", "trixie"],
                              components=["main", "contrib"], arches=["amd64", "arm64"])
    assert snap_compare(_app(lambda: EditSourcePage(src)), terminal_size=SNAP_SIZE)


def test_snapshot_key_modal(snap_compare):
    """The one that rendered its paste box at zero rows on a short terminal."""
    from mmw.tui.key_modal import KeyPage

    assert snap_compare(_app(lambda: KeyPage("debian", "debian-archive.gpg")),
                        terminal_size=SNAP_SIZE)


def test_snapshot_pick_modal(snap_compare):
    from mmw.tui.pick import PickPage

    assert snap_compare(
        _app(lambda: PickPage("Suites", "archive.ubuntu.com publishes 55",
                               [f"suite-{i}" for i in range(55)], ["suite-0"])),
        terminal_size=SNAP_SIZE,
    )


def test_snapshot_bundle_picker(snap_compare, tmp_path):
    from mmw.tui.snapshot_pages import BundlePickPage
    from test_tui_sweep import snapshot_status_demo

    net = config.NetworkConfig(name="lab", select=[])
    assert snap_compare(_app(lambda: BundlePickPage(net, snapshot_status_demo(), tmp_path)),
                        terminal_size=SNAP_SIZE)


def test_snapshot_certs_modal(snap_compare):
    from mmw.tui.publish_modals import CertsPage

    assert snap_compare(
        _app(lambda: CertsPage("lab", [f"host{i}.example.com" for i in range(6)])),
        terminal_size=SNAP_SIZE,
    )


def test_snapshot_bulk_review(snap_compare):
    from mmw.core.draft import SourceDraft
    from mmw.tui.bulk_review import BulkReviewPage

    drafts = [SourceDraft(url=f"https://repo{i}.example.com/ubuntu", provider="apt")
              for i in range(5)]
    assert snap_compare(_app(lambda: BulkReviewPage(drafts)), terminal_size=SNAP_SIZE)


def _demo_config(tmp_path):
    """A config with the shapes that actually stress the manage list: two
    repositories on different hosts, one with an open dimension (so it renders
    as "all — resolved at sync"), and one flat repo with no suite structure."""
    from pathlib import Path

    return config.Config(
        settings=config.Settings(mirror_dir=Path(tmp_path) / "pool"),
        sources=[
            config.SourceConfig(url="https://archive.ubuntu.com/ubuntu",
                                suites=["noble", "noble-updates"],
                                components=["main", "universe"], arches=["amd64"]),
            config.SourceConfig(url="https://ports.ubuntu.com/ubuntu-ports",
                                suites=["noble"], components=["main"], arches=["arm64"]),
            config.SourceConfig(url="https://deb.debian.org/debian",
                                suites=["bookworm"], components=["main"]),
            config.SourceConfig(url="https://packages.microsoft.com/repos/code",
                                provider="apt-flat"),
        ],
        networks=[
            config.NetworkConfig(name="lab", root=Path("/srv/lab"), select=[]),
            config.NetworkConfig(name="dgx", bundle_dir=Path("/media/usb"), select=[]),
        ],
    )


# ── Rebuilt single-app TUI (refactor/tui-overhaul) ──────────────────────
from pathlib import Path as _Path  # noqa: E402

import pytest as _pytest  # noqa: E402


def _mmw_app(tmp_path, section, theme):
    from mmw.tui.app import MmwApp
    cfg = config.Config(settings=config.Settings(
        mirror_dir=_Path("/srv/pool"), max_connections=8, theme=theme))
    return MmwApp(cfg, tmp_path, initial_section=section)


@_pytest.mark.parametrize("theme", ["mmw", "mmw-light"])
@_pytest.mark.parametrize("section", ["settings", "repositories"])
def test_snapshot_mmw_app(snap_compare, tmp_path, section, theme):
    assert snap_compare(_mmw_app(tmp_path, section, theme), terminal_size=SNAP_SIZE)


def test_snapshot_mmw_app_narrow(snap_compare, tmp_path):
    assert snap_compare(_mmw_app(tmp_path, "settings", "mmw"), terminal_size=(80, 24))


def _repos_cfg(theme="mmw"):
    hosts = ["https://deb.example.com/debian", "https://deb.example.com/debian-security",
             "https://ppa.example.org/ubuntu"]
    return config.Config(
        settings=config.Settings(mirror_dir=_Path("/srv/pool"), max_connections=8, theme=theme),
        sources=[config.SourceConfig(url=u, suites=["bookworm"], components=["main"],
                                     arches=["amd64"]) for u in hosts],
    )


@_pytest.fixture
def snap_compare(snap_compare):
    """Every capture ends with `_then_settle` (after the test's own
    `run_before`, if any): no blinking cursor, and the screen given a moment
    to finish rendering — a capture that depends on timing fails under the
    parallel suite's load."""
    def compare(app, *args, run_before=None, **kwargs):
        async def before(pilot):
            if run_before is not None:
                await run_before(pilot)
            await _then_settle(pilot)
        return snap_compare(app, *args, run_before=before, **kwargs)
    return compare


async def _list_ready(pilot, list_id="#repo-list"):
    """Wait for the list's rows themselves — pause counts were a guess that
    lost under parallel load and screenshot a half-built screen."""
    await until(pilot, lambda: len(pilot.app.screen.query(list_id)) == 1
                and len(pilot.app.screen.query_one(list_id).children) >= 2)
    await pilot.app.workers.wait_for_complete()


async def _then_settle(pilot, n=3):
    # A blinking cursor makes the capture depend on timing: under load the
    # screenshot landed in the "off" half of the blink. Not blinking, it shows.
    from textual.widgets import Input, TextArea
    for w in pilot.app.screen.query("Input, TextArea"):
        if isinstance(w, (Input, TextArea)):
            w.cursor_blink = False
    await settle(pilot)


async def _tick_first(pilot):
    await _list_ready(pilot)
    lst = pilot.app.screen.query_one("#repo-list")
    lst.focus()
    lst.index = 1
    await pilot.press("space")
    await until(pilot, lambda: "✓" in str(lst.children[1].query_one("Label").render()))
    await _then_settle(pilot)


@_pytest.mark.parametrize("theme", ["mmw", "mmw-light"])
def test_snapshot_mmw_repositories(snap_compare, tmp_path, theme):
    from mmw.tui.app import MmwApp
    assert snap_compare(MmwApp(_repos_cfg(theme), tmp_path, initial_section="repositories"),
                        terminal_size=SNAP_SIZE, run_before=_tick_first)


def test_snapshot_mmw_edit_source(snap_compare, tmp_path):
    from mmw.tui.app import MmwApp
    from mmw.tui.source_screens import EditSourcePage

    async def open_edit(pilot):
        await _list_ready(pilot)
        pilot.app.push_screen(EditSourcePage(_repos_cfg().sources[0]))
        await until(pilot, lambda: isinstance(pilot.app.screen, EditSourcePage)
                    and pilot.app.screen.query("#edit-source-url"))
        await _then_settle(pilot)

    assert snap_compare(MmwApp(_repos_cfg(), tmp_path, initial_section="repositories"),
                        terminal_size=(80, 24), run_before=open_edit)


async def _open_add(pilot):
    from mmw.tui.add_source import AddSourcePage
    await _list_ready(pilot)
    pilot.app.screen.query_one("#repo-list").focus()
    await pilot.press("a")
    await until(pilot, lambda: isinstance(pilot.app.screen, AddSourcePage)
                and pilot.app.screen.query("#add-entry-list"))


def test_snapshot_mmw_add_source(snap_compare, tmp_path):
    """Add starts by asking what you are adding."""
    from mmw.tui.app import MmwApp

    async def open_add(pilot):
        await _open_add(pilot)
        await _then_settle(pilot)

    assert snap_compare(MmwApp(_repos_cfg(), tmp_path, initial_section="repositories"),
                        terminal_size=(80, 24), run_before=open_add)


def test_snapshot_mmw_add_source_apt_step(snap_compare, tmp_path):
    """…then the apt entry's step."""
    from mmw.tui.app import MmwApp
    from pickers import choose_add_entry

    async def open_apt(pilot):
        await _open_add(pilot)
        await choose_add_entry(pilot.app, pilot)
        await until(pilot, lambda: pilot.app.screen.query("#mode-select"))
        await _then_settle(pilot)

    assert snap_compare(MmwApp(_repos_cfg(), tmp_path, initial_section="repositories"),
                        terminal_size=(80, 24), run_before=open_apt)


def _nets_cfg(theme="mmw"):
    cfg = _repos_cfg(theme)
    cfg.networks = [config.NetworkConfig(name=n, root=_Path(f"/srv/{n}"),
                                         select=[config.NetworkSelect(source=cfg.sources[0].url)])
                    for n in ("dgx", "lab")]
    return cfg


@_pytest.mark.parametrize("theme", ["mmw", "mmw-light"])
def test_snapshot_mmw_networks(snap_compare, tmp_path, theme):
    from mmw.tui.app import MmwApp

    async def settle(pilot):
        from mmw.tui.sections.network_detail import NetworkDetail
        await _list_ready(pilot, "#net-list")
        await until(pilot, lambda: pilot.app.screen.query(NetworkDetail)
                    and pilot.app.screen.query_one(NetworkDetail)._selected is not None)
        await pilot.app.workers.wait_for_complete()
        await _then_settle(pilot)

    assert snap_compare(MmwApp(_nets_cfg(theme), tmp_path, initial_section="networks"),
                        terminal_size=SNAP_SIZE, run_before=settle)


def test_snapshot_mmw_edit_network(snap_compare, tmp_path):
    from mmw.tui.network_screens import EditNetworkPage

    net = config.NetworkConfig(name="lab", root=_Path("/srv/lab"), select=[])
    assert snap_compare(_app(lambda: EditNetworkPage(net, _mirrors())), terminal_size=(80, 24))


def test_snapshot_mmw_sync_idle(snap_compare, tmp_path):
    from mmw.tui.app import MmwApp
    assert snap_compare(MmwApp(_repos_cfg(), tmp_path, initial_section="sync"),
                        terminal_size=SNAP_SIZE)


def test_snapshot_mmw_publish_idle(snap_compare, tmp_path):
    from mmw.tui.app import MmwApp
    assert snap_compare(MmwApp(_repos_cfg(), tmp_path, initial_section="publish"),
                        terminal_size=SNAP_SIZE)
