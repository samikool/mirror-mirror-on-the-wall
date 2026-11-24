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
from waiting import shows, until
from test_transfer_repository_selection import synced  # noqa: F401


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




def test_p_into_another_directory(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    other = tmp_path / "elsewhere"

    async def check(app, pilot):
        await pilot.press("p")
        await until(pilot, lambda: type(app.screen).__name__ == "PublishPickPage" and app.screen.query("#pick-dir"))
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
        await until(pilot, lambda: type(app.screen).__name__ == "PublishPickPage" and app.screen.query("#pick-dir"))
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


def test_b_bundles_between_point_dates_with_an_estimate(two_mirrors, tmp_path, monkeypatch):  # noqa: F811
    from mmw.core import points, snapshots
    from mmw.core.publisher.bundle import bundle_network
    from test_site_apply_timeline import set_a
    cfg, net, mirror_dir = two_mirrors
    out = tmp_path / "usb"
    net = net.model_copy(update={"bundle_dir": out})
    cfg = cfg.model_copy(update={"networks": [net]})
    set_a(mirror_dir, 1)
    monkeypatch.setattr(snapshots, "now_stamp", lambda: "20261101T120000Z")
    bundle_network(net, cfg, mirror_dir, out_path=tmp_path / "first.tar")
    set_a(mirror_dir, 2)
    points.record_network_points(net, cfg, mirror_dir, stamp="20261102T120000Z")
    monkeypatch.setattr(snapshots, "now_stamp", lambda: "20261102T120000Z")

    async def check(app, pilot):
        await pilot.press("t")
        await until(pilot, lambda: type(app.screen).__name__ == "BundlePickPage" and app.screen.query("#pick-from"))
        assert app.screen.query_one("#pick-from", Select).value == "last"
        app.screen.query_one("#pick-from", Select).value = "date"
        app.screen.query_one("#pick-since", Input).value = "2026-11-01"
        app.screen.query_one("#pick-to", Select).value = "date"
        app.screen.query_one("#pick-until", Input).value = "2026-11-02"
        app.screen.query_one("#pick-installer", Select).value = "no"
        await shows(pilot, lambda: app.screen.query_one("#pick-estimate").render(), "Sends about", "new blobs")
        await confirm_picker(app, pilot, "BundlePickPage")
        await until(pilot, lambda: _finished(app))
        assert app.active_transfer.outcome == "ok", app.notes
    _run(cfg, mirror_dir.parent, check)
    assert len(list(out.glob("*.tar"))) == 1
    with Pool(mirror_dir, read_only=True) as pool:
        assert pool.snapshots(net.name) == []


def test_b_refuses_an_invalid_point_target_date(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    cfg = cfg.model_copy(update={"networks": [net.model_copy(update={"bundle_dir": tmp_path / "usb"})]})

    async def check(app, pilot):
        await pilot.press("t")
        await until(pilot, lambda: type(app.screen).__name__ == "BundlePickPage"
                    and app.screen.query("#pick-to") and app.screen.query("#btn-pick-go"))
        app.screen.query_one("#pick-to", Select).value = "date"
        app.screen.query_one("#pick-until", Input).value = "not-a-date"
        await pilot.pause()
        app.screen.query_one("#btn-pick-go").press()
        await until(pilot, lambda: "Date:" in str(app.screen.query_one("#page-status").render()))
        assert type(app.screen).__name__ == "BundlePickPage"
    _run(cfg, mirror_dir.parent, check)


def _rows(app) -> list[tuple]:
    table = app.screen.query_one("#snap-table", DataTable)
    return [tuple(str(c) for c in table.get_row_at(i)) for i in range(table.row_count)]







def test_the_detail_pane_says_what_the_root_serves(two_mirrors):  # noqa: F811
    from mmw.core.publisher import publish_snapshot
    cfg, net, mirror_dir = two_mirrors
    take_snapshot(net, cfg, mirror_dir, label="golden")
    publish_snapshot(net, cfg, mirror_dir, "golden")

    async def check(app, pilot):
        await until(pilot, lambda: any(
            "serving" in str(lbl.render()) for lbl in app.screen.query(".detail-meta")))
        line = next(str(lbl.render()) for lbl in app.screen.query(".detail-meta")
                    if "Activity" in str(lbl.render()))
        assert "serving" in line and "golden" in line
    _run(cfg, mirror_dir.parent, check)












def _mmw_net(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    net = net.model_copy(update={"bundle_dir": tmp_path / "usb"})
    return cfg.model_copy(update={"networks": [net]}), net, mirror_dir


def test_bundle_pick_mmw_has_repos_and_stage(two_mirrors, tmp_path):  # noqa: F811
    from textual.widgets import SelectionList
    cfg, net, mirror_dir = _mmw_net(two_mirrors, tmp_path)
    from mmw.core import points
    points.record_network_points(net, cfg, mirror_dir)
    key_b = next(s.identity for s in cfg.sources if s.name == "b")

    async def check(app, pilot):
        await pilot.press("t")
        await until(pilot, lambda: type(app.screen).__name__ == "BundlePickPage"
                    and app.screen.query("#pick-repos"))
        repos = app.screen.query_one("#pick-repos", SelectionList)
        assert set(repos.selected) == {s.identity for s in cfg.sources}           # all ticked
        repos.deselect(key_b)
        app.screen.query_one("#pick-as", Select).value = "stage"
        app.screen.query_one("#pick-installer", Select).value = "no"      # stage only: no installer build
        await until(pilot, lambda: "new blobs" in str(app.screen.query_one("#pick-estimate").render()))
        await confirm_picker(app, pilot, "BundlePickPage")
        await until(pilot, lambda: _finished(app))
        assert app.active_transfer.outcome == "ok", app.notes
    _run(cfg, mirror_dir.parent, check)
    (staged,) = (tmp_path / "usb").iterdir()
    assert staged.is_dir()
    import json
    header = json.loads((staged / ".mmw/manifest.jsonl").read_text().splitlines()[0])
    assert {m["label"] for m in header["points"].values()} == {"a"}       # b left out


def test_activity_and_transfer_from_read_dates(synced, tmp_path, monkeypatch):  # noqa: F811
    """Activity: `last transferred 23 Nov (points to 01 Nov)`; From: the date, never a stamp."""
    import re
    from mmw.core import snapshots
    from mmw.core.publisher.bundle import bundle_network
    from mmw.site.versionref import short_day
    cfg, net, mirror_dir, _units = synced
    monkeypatch.setattr(snapshots, "now_stamp", lambda: "20261123T015441Z")
    bundle_network(net, cfg, mirror_dir, out_path=tmp_path / "first.tar")
    from mmw.core.pool import Pool
    with Pool(mirror_dir) as pool:
        made = pool.bundles(net.name)[0].made        # the pool's own clock, not the patched one

    async def check(app, pilot):
        def activity():
            return next((str(lbl.render()) for lbl in app.screen.query(".detail-meta")
                         if "Activity" in str(lbl.render()) and "transferred" in str(lbl.render())), "")
        await until(pilot, activity)
        nov23, nov01 = short_day("20261123T015441Z"), short_day("20261101T000000Z")
        assert f"last transferred {short_day(made)} (points to {nov01})" in activity(), activity()
        assert not re.search(r"\d{8}T\d{6}Z", activity())
        await pilot.press("t")
        await until(pilot, lambda: type(app.screen).__name__ == "BundlePickPage" and app.screen.query("#pick-from"))
        labels = [str(p) for p, _v in app.screen.query_one("#pick-from", Select)._options]
        assert labels[0] == f"Where the last transfer went — {nov23}", labels
        assert not any(re.search(r"\d{8}T\d{6}Z", label) for label in labels), labels
    _run(cfg, tmp_path, check)
