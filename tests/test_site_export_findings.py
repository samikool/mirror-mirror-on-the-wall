"""Findings from the far-side export branch's final review, each pinned."""
import io
import json

import pytest
from site_apt_rows import Repo
from site_format3 import write_format3

from mmw.site.apply import ApplyError, apply_bundle, apply_to_set
from mmw.site.db import SiteDB
from mmw.site.export import plan_root_export
from mmw.site.mset import open_set
from mmw.site.tracks import split_tracks

from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply_format3 import _c_mirror, flat_point, oak_set  # noqa: F401
from test_site_apply_timeline import _apply, _bundle, env, set_a  # noqa: F401
from test_site_export_plan import KEY, site  # noqa: F401
from test_site_export_write import one_root  # noqa: F401
from test_snapshot_repo_versions import _key

quiet = lambda *a: None  # noqa: E731


@pytest.fixture(autouse=True)
def _xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


# C1 ── no manifest row may name a path outside the root, in any format

@pytest.mark.parametrize("bad", ["example.com/a/../../../escape", "/tmp/escape"])
def test_a_format3_row_outside_the_root_is_refused(env, tmp_path, bad):  # noqa: F811
    cfg, net, md, home = env
    _apply(home, _bundle(cfg, net, md, tmp_path / "b1", from_ref=None))
    draft, blobs = flat_point(2)

    evil = draft.__class__(draft.track, draft.ident, draft.date, draft.fields,
                           {**draft.files, bad: draft.files["example.com/a/x2.deb"]})
    path = write_format3(tmp_path / "evil.tar", "elm", {_key(cfg, "a"): ("a", ["example.com/a"],
                                                                          [(evil, "20261102T1200Z", None)])}, blobs)
    with pytest.raises(ApplyError, match="unsafe path") as e:
        apply_bundle(home, path, say=quiet)
    assert e.value.code == 2 and not (tmp_path / "escape").exists()


def test_a_format2_manifest_only_row_outside_the_root_is_refused(env, tmp_path):  # noqa: F811
    from test_site_apply import _rewrite
    cfg, net, md, home = env
    src = _bundle(cfg, net, md, tmp_path / "b1", from_ref=None)

    def add_row(member, data):
        if member.name == ".mmw/manifest.jsonl":
            return data + (json.dumps({"p": "../../escape", "c": "0" * 64}) + "\n").encode()
        return data
    bad = _rewrite(src, tmp_path / "bad.tar", add_row)
    with pytest.raises(ApplyError, match="unsafe path"):
        apply_bundle(home, str(bad), say=quiet)


# I1 ── every row of an export carries its size, sent before or not

def test_an_incremental_export_says_every_size(site):  # noqa: F811
    from mmw.site.export import root_export_plan
    home, _store = site
    first = plan_root_export(home, "elm", repos=None, mode="new", since=None)
    with SiteDB(home) as db:
        db.record_export("elm", [(KEY, p.ident, files) for rx in first for _t, p, files in rx.points
                                 if p.date < "20261103"], "20261115T000000Z")
    plan = root_export_plan(home, "elm", repos=None, mode="new", since=None)
    every = {c for rx in plan["repos"] for pt in rx["points"] for c in pt["files"].values()}
    assert set(plan["send"]) < every                        # p1.deb, p2.deb went before …
    assert all(plan["sizes"].get(c, 0) > 0 for c in every)  # … and still say their size


# I2 ── a point that gained files after it was sent goes again

def test_a_point_widened_after_export_goes_again(site):  # noqa: F811
    home, store = site
    from mmw.site.timeline import import_rows
    r = Repo()
    pkgs = [r.deb(f"pool/q{i}.deb") for i in (1, 2)]
    r.suite("trixie", "09 Nov 2026 00:00:00 UTC", pkgs)
    for cs, data in r.blobs.items():
        store.add_stream(io.BytesIO(data), expect=cs)
    narrow = {p: c for p, c in r.rows.items() if not p.endswith("q2.deb")}
    with SiteDB(home) as db:
        import_rows(db, store, KEY, "debian", [r.base], narrow, {c: len(b) for c, b in r.blobs.items()},
                    "20261109T120000Z", None, set())
    first = plan_root_export(home, "elm", repos=None, mode="new", since=None)
    with SiteDB(home) as db:
        db.record_export("elm", [(KEY, p.ident, files) for rx in first for _t, p, files in rx.points],
                         "20261115T000000Z")
        import_rows(db, store, KEY, "debian", [r.base], r.rows, {c: len(b) for c, b in r.blobs.items()},
                    "20261110T120000Z", None, set())                      # the same Release, widened
    again = plan_root_export(home, "elm", repos=None, mode="new", since=None)
    ((_t, p, files),) = [pt for rx in again for pt in rx.points]
    assert "ex.org/debian/pool/q2.deb" in files
    from mmw.site.export import export_blobs
    assert files["ex.org/debian/pool/q2.deb"] in export_blobs(home, "elm", again, "new")


# I3 ── an incomplete point from another far side says how to complete it there

def test_an_incomplete_export_point_names_the_far_side_command(env, tmp_path):  # noqa: F811
    cfg, net, md, home = env
    _apply(home, _bundle(cfg, net, md, tmp_path / "b1", from_ref=None))
    draft, blobs = flat_point(2)
    blobs.pop(draft.files["example.com/a/x2.deb"])                          # assumed already there
    path = write_format3(tmp_path / "p.tar", "elm", {_key(cfg, "a"): ("a", ["example.com/a"],
                                                                       [(draft, "20261102T1200Z", None)])}, blobs)
    r = apply_bundle(home, path, say=quiet)
    line = next(ln for ln in r.lines if "incomplete" in ln)
    assert "on elm's far side: mmw site bundle --to oak --repo 'a' --all" in line


# I4 ── a deployed repository's keys never hide its other files

def test_deployed_keys_and_loose_files_are_both_served(oak_set, tmp_path):  # noqa: F811
    cfg, root = oak_set
    key, mirror = _c_mirror()
    loose = Repo("example.net/c")
    loose.put("c.deb", b"C" * 50)
    (draft,) = split_tracks(loose.rows, ["example.net/c"], loose.read, "20261105T000000Z")
    path = write_format3(tmp_path / "c.tar", "elm", {key: ("c", ["example.net/c"], [(draft, "20261105T1200Z", None)])},
                         loose.blobs, network_files={"keys/c/c.asc": b"KEY"},
                         settings=json.dumps({"network": {"name": "elm"}, "mirrors": [mirror]}))
    apply_to_set(open_set([str(root)]), path, deploy={"c"}, place={"example.net": str(root)}, say=quiet)
    assert (root / "example.net/c/c.deb").exists() and (root / "keys/c/c.asc").exists()


# I5, M1 ── the TUI works the plan out only when what goes changes; none ticked is said

def test_export_page_rechecks_only_when_what_goes_changes(one_root, tmp_path, monkeypatch):  # noqa: F811
    import asyncio

    from textual.widgets import Input, SelectionList, Static

    from mmw.site import export
    from mmw.tui.site.app import SiteApp
    from waiting import until
    calls = []
    real = export.export_bundle
    monkeypatch.setattr(export, "export_bundle", lambda *a, **k: calls.append(k.get("mode")) or real(*a, **k))

    async def go():
        app = SiteApp(open_set())
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage")
            await until(pilot, lambda: app.screen.query_one("#site-repos").row_count == 2)
            await pilot.press("x")
            await until(pilot, lambda: type(app.screen).__name__ == "ExportPage" and app.screen.query("#export-repos"))
            page = app.screen
            page.query_one("#export-target", Input).value = "elm"
            await until(pilot, lambda: "would send" in str(page.query_one("#export-would", Static).render()))
            before = len(calls)
            page.query_one("#export-out", Input).value = str(tmp_path / "x.tar")
            await pilot.pause(0.6)
            assert len(calls) == before                    # where it's written changes nothing sent
            page.query_one("#export-repos", SelectionList).deselect_all()
            await until(pilot, lambda: "tick at least one" in str(page.query_one("#export-would", Static).render()))
    asyncio.run(go())
