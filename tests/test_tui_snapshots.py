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
    from textual.app import App, ComposeResult

    class _Host(App):
        def compose(self) -> ComposeResult:
            yield from ()

        def on_mount(self):
            self.push_screen(factory())

    return _Host()


# `snap_compare` wants a path or an App instance; these fixtures hand it the
# latter so each screen can be constructed with the data that makes it
# interesting rather than whatever a module-level app would have.

def test_snapshot_edit_network(snap_compare):
    from mmw.tui.network_screens import EditNetworkScreen

    net = config.NetworkConfig(name="lab", select=[])
    assert snap_compare(_app(lambda: EditNetworkScreen(net, _mirrors())),
                        terminal_size=SNAP_SIZE)


def test_snapshot_edit_source(snap_compare):
    from mmw.tui.source_screens import EditSourceScreen

    src = config.SourceConfig(url="https://deb.example.com/debian",
                              suites=["bookworm", "trixie"],
                              components=["main", "contrib"], arches=["amd64", "arm64"])
    assert snap_compare(_app(lambda: EditSourceScreen(src)), terminal_size=SNAP_SIZE)


def test_snapshot_key_modal(snap_compare):
    """The one that rendered its paste box at zero rows on a short terminal."""
    from mmw.tui.key_modal import KeyModal

    assert snap_compare(_app(lambda: KeyModal("debian", "debian-archive.gpg")),
                        terminal_size=SNAP_SIZE)


def test_snapshot_pick_modal(snap_compare):
    from mmw.tui.pick import PickModal

    assert snap_compare(
        _app(lambda: PickModal("Suites", "archive.ubuntu.com publishes 55",
                               [f"suite-{i}" for i in range(55)], ["suite-0"])),
        terminal_size=SNAP_SIZE,
    )


def test_snapshot_bundle_mode_modal(snap_compare):
    from mmw.tui.publish_modals import BundleModeModal

    assert snap_compare(_app(lambda: BundleModeModal("lab")), terminal_size=SNAP_SIZE)


def test_snapshot_certs_modal(snap_compare):
    from mmw.tui.publish_modals import CertsModal

    assert snap_compare(
        _app(lambda: CertsModal("lab", [f"host{i}.example.com" for i in range(6)])),
        terminal_size=SNAP_SIZE,
    )


def test_snapshot_bulk_review(snap_compare):
    from mmw.core.draft import RepositoryDraft
    from mmw.tui.bulk_review import BulkReviewScreen

    drafts = [RepositoryDraft(url=f"https://repo{i}.example.com/ubuntu", provider="apt")
              for i in range(5)]
    assert snap_compare(_app(lambda: BulkReviewScreen(drafts)), terminal_size=SNAP_SIZE)


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


def test_snapshot_manage_tab(snap_compare, tmp_path):
    """The highest-traffic screen in the app, and the one where a fixed 36-column
    list panel hard-cut every row mid-word."""
    from pathlib import Path

    from mmw.tui.manage import ManageApp

    app = ManageApp(_demo_config(tmp_path), Path("."), initial_tab="tab-manage")
    assert snap_compare(app, terminal_size=SNAP_SIZE)


def test_snapshot_networks_tab(snap_compare, tmp_path):
    """Seven action buttons and the r/b bindings — the surface completed last."""
    from pathlib import Path

    from mmw.tui.manage import ManageApp

    app = ManageApp(_demo_config(tmp_path), Path("."), initial_tab="tab-networks")
    assert snap_compare(app, terminal_size=SNAP_SIZE)
