"""Snapshots in the TUI: `p`/`b` pickers on Networks, the Snapshots page (`n`),
and the detail pane's snapshot line. Real pool, real publish/bundle into tmp."""
import asyncio
import hashlib

from textual.widgets import DataTable, Input, ListView, Select

from mmw.core import config
from mmw.core.pool import KIND_META, KIND_PKG, Pool
from mmw.core.snapshots import take_snapshot
from mmw.tui.app import MmwApp

from pickers import confirm_picker
from test_dry_run_totals import SHARED, _index, two_mirrors  # noqa: F401 — the shared pool fixture
from waiting import until


def _move_on(mirror_dir):
    """Upstream replaces a.deb with a2.deb: the live state no longer has a.deb."""
    files = {"shared.deb": SHARED, "a2.deb": b"n" * 300}
    with Pool(mirror_dir) as pool:
        refs = []
        for fname, data in {**files, "Packages": _index(files)}.items():
            cs = hashlib.sha256(data).hexdigest()
            pool.add(data, cs, "sha256", fname, len(data))
            refs.append((cs, fname, KIND_META if fname == "Packages" else KIND_PKG))
        pool.reconcile_refs("a", refs)


def _run(cfg, cfg_dir, check):
    config.save(cfg, cfg_dir)

    async def go():
        app = MmwApp(cfg, cfg_dir, initial_section="networks")
        app.notes = []
        app.notify = lambda m, **_: app.notes.append(m)
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: app.screen.query("#net-list"))
            app.screen.query_one("#net-list", ListView).focus()
            await check(app, pilot)
            if app.active_transfer is not None and app.active_transfer.running:
                app.active_transfer.stop()
                await until(pilot, lambda: not app.active_transfer.running)
    asyncio.run(go())


def _finished(app):
    view = app.active_transfer
    return view is not None and not view.running and view.screen is not None and str(
        view.screen.query_one("#page-status").render()) == view.summary()


def test_p_publishes_an_older_snapshot(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    s1 = take_snapshot(net, cfg, mirror_dir, label="before")
    _move_on(mirror_dir)

    async def check(app, pilot):
        await pilot.press("p")
        await until(pilot, lambda: type(app.screen).__name__ == "PublishPickPage")
        assert "serves" not in str(app.screen.query_one("#page-body").render())
        app.screen.query_one("#pick-target", Select).value = s1.stamp
        await confirm_picker(app, pilot, "PublishPickPage")
        await until(pilot, lambda: _finished(app))
        assert app.active_transfer.outcome == "ok", app.notes
    _run(cfg, mirror_dir.parent, check)
    assert (net.root / "example.com" / "a" / "a.deb").exists()
    assert not (net.root / "example.com" / "a" / "a2.deb").exists()
    with Pool(mirror_dir, read_only=True) as pool:
        assert [p.target for p in pool.published("oak")] == [s1.stamp]


def test_p_into_another_directory(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    other = tmp_path / "elsewhere"

    async def check(app, pilot):
        await pilot.press("p")
        await until(pilot, lambda: type(app.screen).__name__ == "PublishPickPage")
        app.screen.query_one("#pick-dir", Select).value = "other"
        await until(pilot, lambda: app.screen.query_one("#field-pick-root").display)
        app.screen.query_one("#pick-root", Input).value = str(other)
        await confirm_picker(app, pilot, "PublishPickPage")
        await until(pilot, lambda: _finished(app))
        assert app.active_transfer.outcome == "ok", app.notes
    _run(cfg, mirror_dir.parent, check)
    assert (other / "example.com" / "b" / "b.deb").exists()
    assert not net.root.exists()


def test_p_into_another_directory_needs_one(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors

    async def check(app, pilot):
        await pilot.press("p")
        await until(pilot, lambda: type(app.screen).__name__ == "PublishPickPage")
        app.screen.query_one("#pick-dir", Select).value = "other"
        await pilot.pause()
        app.screen.query_one("#btn-pick-go").press()
        await pilot.pause()
        assert type(app.screen).__name__ == "PublishPickPage"
        assert "Choose the directory" in str(app.screen.query_one("#page-status").render())
        await pilot.press("escape")
        await until(pilot, lambda: app.current_mode == "networks"
                    and type(app.screen).__name__ == "NetworksPage")
        assert app.active_transfer is None
    _run(cfg, mirror_dir.parent, check)


def test_b_bundles_between_two_snapshots_with_an_estimate(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    out = tmp_path / "usb"
    cfg = cfg.model_copy(update={"networks": [net.model_copy(update={"bundle_dir": out})]})
    s1 = take_snapshot(net, cfg, mirror_dir, label="before")
    _move_on(mirror_dir)
    s2 = take_snapshot(net, cfg, mirror_dir, label="after")

    async def check(app, pilot):
        await pilot.press("b")
        await until(pilot, lambda: type(app.screen).__name__ == "BundlePickPage")
        app.screen.query_one("#pick-from", Select).value = s1.stamp
        app.screen.query_one("#pick-to", Select).value = s2.stamp
        await until(pilot, lambda: "Sends about" in str(
            app.screen.query_one("#pick-estimate").render()))
        # a2.deb, the new index and its by-hash link
        assert "3 files" in str(app.screen.query_one("#pick-estimate").render())
        await confirm_picker(app, pilot, "BundlePickPage")
        await until(pilot, lambda: _finished(app))
        assert app.active_transfer.outcome == "ok", app.notes
    _run(cfg, mirror_dir.parent, check)
    assert [p.name for p in out.glob("*.tar*") if not p.name.endswith(".sha256")] == [
        f"oak-{s1.stamp}-to-{s2.stamp}.tar"]


def test_b_refuses_the_same_snapshot_at_both_ends(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    cfg = cfg.model_copy(update={"networks": [net.model_copy(
        update={"bundle_dir": tmp_path / "usb"})]})
    s1 = take_snapshot(net, cfg, mirror_dir)

    async def check(app, pilot):
        await pilot.press("b")
        await until(pilot, lambda: type(app.screen).__name__ == "BundlePickPage")
        app.screen.query_one("#pick-from", Select).value = s1.stamp
        app.screen.query_one("#pick-to", Select).value = s1.stamp
        await pilot.pause()
        app.screen.query_one("#btn-pick-go").press()
        await pilot.pause()
        assert type(app.screen).__name__ == "BundlePickPage"
        assert "same snapshot" in str(app.screen.query_one("#page-status").render())
    _run(cfg, mirror_dir.parent, check)


def _rows(app) -> list[tuple]:
    table = app.screen.query_one("#snap-table", DataTable)
    return [tuple(str(c) for c in table.get_row_at(i)) for i in range(table.row_count)]


def test_the_snapshots_page_takes_renames_deletes_and_cleans(two_mirrors):  # noqa: F811
    from mmw.core.publisher import publish_snapshot
    cfg, net, mirror_dir = two_mirrors
    served = take_snapshot(net, cfg, mirror_dir, label="served")
    publish_snapshot(net, cfg, mirror_dir, "served")
    take_snapshot(net, cfg, mirror_dir, label="old")

    async def check(app, pilot):
        await pilot.press("n")
        await until(pilot, lambda: type(app.screen).__name__ == "SnapshotsPage"
                    and len(_rows(app)) == 2)
        assert "published" in _rows(app)[1][5]
        await until(pilot, lambda: all(r[4] != "…" for r in _rows(app)))   # holds filled in

        # Take — in the background, labelled.
        await pilot.press("t")
        await until(pilot, lambda: type(app.screen).__name__ == "LabelPage")
        app.screen.query_one("#inp-snap-label", Input).value = "fresh"
        await pilot.press("enter")
        await until(pilot, lambda: type(app.screen).__name__ == "SnapshotsPage"
                    and len(_rows(app)) == 3)
        assert _rows(app)[0][1] == "fresh"
        assert any("taken" in n for n in app.notes)

        # Rename the newest.
        await pilot.press("r")
        await until(pilot, lambda: type(app.screen).__name__ == "LabelPage")
        app.screen.query_one("#inp-snap-label", Input).value = "renamed"
        await pilot.press("enter")
        await until(pilot, lambda: type(app.screen).__name__ == "SnapshotsPage"
                    and _rows(app) and _rows(app)[0][1] == "renamed")

        # Deleting the served one is refused.
        app.screen.query_one("#snap-table", DataTable).move_cursor(row=2)
        await pilot.press("d")
        await until(pilot, lambda: any("is published at" in n for n in app.notes))
        assert len(_rows(app)) == 3

        # Clean up, keeping none: the served one stays.
        await pilot.press("c")
        await until(pilot, lambda: type(app.screen).__name__ == "CleanPage")
        app.screen.query_one("#inp-snap-keep", Input).value = "0"
        await until(pilot, lambda: "Deletes" in str(
            app.screen.query_one("#clean-preview").render()))
        app.screen.query_one("#btn-clean-go").press()
        await until(pilot, lambda: type(app.screen).__name__ == "SnapshotsPage"
                    and len(_rows(app)) == 1)
        assert _rows(app)[0][0] == served.stamp
    _run(cfg, mirror_dir.parent, check)


def test_the_detail_pane_says_what_the_root_serves(two_mirrors):  # noqa: F811
    from mmw.core.publisher import publish_snapshot
    cfg, net, mirror_dir = two_mirrors
    take_snapshot(net, cfg, mirror_dir, label="golden")
    publish_snapshot(net, cfg, mirror_dir, "golden")

    async def check(app, pilot):
        await until(pilot, lambda: any(
            "serving" in str(lbl.render()) for lbl in app.screen.query(".detail-meta")))
        line = next(str(lbl.render()) for lbl in app.screen.query(".detail-meta")
                    if "Snapshots" in str(lbl.render()))
        assert "1 · serving" in line and "golden" in line
    _run(cfg, mirror_dir.parent, check)


def test_the_snapshots_button_opens_the_page(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors

    async def check(app, pilot):
        app.screen.query_one("#btn-net-snapshots").press()
        await until(pilot, lambda: type(app.screen).__name__ == "SnapshotsPage")
        assert app.screen.subtitle == "oak"
    _run(cfg, mirror_dir.parent, check)


def test_leaving_the_snapshots_page_open_at_exit_never_crashes(two_mirrors):  # noqa: F811
    """The network pane's stats load could land while the app shut down; the
    rebuild mounted beside children removal could no longer detach (they
    never started) and crashed on a duplicate `#stats-progress`. It took a
    few exits to hit — hence the loop."""
    cfg, net, mirror_dir = two_mirrors

    async def check(app, pilot):
        app.screen.query_one("#btn-net-snapshots").press()
        await until(pilot, lambda: type(app.screen).__name__ == "SnapshotsPage")
    for _ in range(12):
        _run(cfg, mirror_dir.parent, check)
