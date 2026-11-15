"""Findings from the timelines branch's final review, each pinned by a test."""
import io

import pytest
from site_apt_rows import Repo

from mmw.site import home as H
from mmw.site import ops
from mmw.site.apply import apply_bundle, plan_apply
from mmw.site.db import SiteDB
from mmw.site.roots import Roots
from mmw.site.timeline import choose, ensure_state, import_rows

from site_points import nth
from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply import _bundle, env  # noqa: F401
from test_site_ops import _bump
from test_snapshot_repo_versions import _change_a, _key

quiet = lambda *a: None  # noqa: E731


# 1 ── the TUI review decides "unchanged" by content, not by version numbers

def test_review_unchanged_is_by_content_not_numbers(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet)
    _change_a(md)
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "2")), say=quiet)
    ops.serve(home, "a", nth(home, "a", 1))          # the site's state numbers run ahead …
    ops.forward(home, "a")
    _bump(md)                                        # … of the connected side's, which sends v3
    plan = plan_apply(home, str(_bundle(cfg, net, md, tmp_path / "3")))
    row = next(r for r in plan.rows if r.repo == _key(cfg, "a"))
    assert not row.unchanged
    again = plan_apply(home, str(_bundle(cfg, net, md, tmp_path / "1b", from_ref=None)))
    assert next(r for r in again.rows if r.repo == _key(cfg, "b")).unchanged


# ── a site with synthetic dated repositories ─────────────────────────────────

@pytest.fixture
def site(tmp_path):
    home = H.init_root(tmp_path / "srv", name="main", set_id="s1", network="oak")
    roots = Roots(H.load_site(home))
    with SiteDB(home) as db:
        yield home, db, roots.store(roots.primary), roots


def _bring(db, store, repo, r: Repo, stamp: str, key="https://ex.org/debian/"):
    for cs, data in r.blobs.items():
        store.add_stream(io.BytesIO(data), expect=cs)
    return import_rows(db, store, key, "debian", [r.base], r.rows, {c: len(b) for c, b in r.blobs.items()},
                       stamp, None, set())


def _served(db, store, key="https://ex.org/debian/"):
    chosen, _w = choose(db, store, key)
    vid, _w = ensure_state(db, store, key, chosen)
    return set(db.version_files(vid))


# 2 ── a suite no longer in the bundle is no longer served

def test_a_suite_dropped_from_the_selection_is_not_served(site):
    _home, db, store, _roots = site
    both = Repo()
    both.suite("old", "Sat, 01 Nov 2026 00:00:00 UTC", [])
    both.suite("new", "Sat, 01 Nov 2026 00:00:00 UTC", [])
    _bring(db, store, None, both, "20261101T000000Z")
    only = Repo()
    only.suite("new", "Sun, 02 Nov 2026 00:00:00 UTC", [])
    _bring(db, store, None, only, "20261102T000000Z")
    served = _served(db, store)
    assert "ex.org/debian/dists/new/Release" in served
    assert not any("/dists/old/" in p for p in served)


# 3 ── a Release-less repository whose content returns to an earlier state

def test_content_that_comes_back_is_served_again(site):
    _home, db, store, _roots = site
    for i, files in enumerate(({"a": b"A", "b": b"B"}, {"a": b"A"}, {"a": b"A", "b": b"B"}), 1):
        r = Repo("example.com/p")
        for name, data in files.items():
            r.put(name, data)
        _bring(db, store, None, r, f"2026110{i}T000000Z", key="P")
    assert _served(db, store, key="P") == {"example.com/p/a", "example.com/p/b"}


# 4 ── an upgraded site shows its timeline before anything writes

def test_status_and_history_migrate_an_upgraded_site(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet)
    with SiteDB(home) as db:                         # as a site from before timelines
        for table in ("point_files", "points", "tracks"):
            db._conn.execute(f"DELETE FROM {table}")
        db._conn.execute("DELETE FROM meta WHERE key = 'timeline'")
        db._conn.commit()
    ((_t, rows),) = ops.history(home, "a")
    assert [mark for *_x, mark in rows] == ["serving"]


# 5 ── choosing stops at the first complete point

def test_choose_checks_no_further_than_it_needs(site, monkeypatch):
    _home, db, store, _roots = site
    for day in range(1, 6):
        r = Repo()
        r.deb(f"pool/p{day}.deb")
        r.suite("sid", f"0{day} Nov 2026 00:00:00 UTC", [f"pool/p{day}.deb"])
        _bring(db, store, None, r, f"2026110{day}T000000Z")
    from mmw.site import timeline
    calls = []
    real = timeline.is_complete
    monkeypatch.setattr(timeline, "is_complete", lambda *a, **k: calls.append(a[2]) or real(*a, **k))
    choose(db, store, "https://ex.org/debian/")
    assert len(calls) == 1


# 7 ── a Release Date: no clock can hold never stops an apply

def test_an_impossible_release_date_falls_back():
    from mmw.site.tracks import release_date
    for value in ("Fri, 31 Dec 9999 23:00:00 -0100", "Wed, 01 Jan 1800 00:00:00 UTC"):
        stamp, warning = release_date(f"Date: {value}\n", "20261114T000000Z")
        assert stamp == "20261114T000000Z" and warning


# 8 ── a point is named so it can never read as a date

def test_digests_that_look_like_dates_still_name_their_point():
    from mmw.site.db import PointInfo
    from mmw.site.versionref import name_of, resolve_point
    tricky = PointInfo(1, 1, "release:123dec" + "0" * 58, "20261101T000000Z", "20261101T000000Z", None, True)
    other = PointInfo(2, 1, "release:" + "f" * 64, "20261215T000000Z", "20261215T000000Z", None, True)
    assert resolve_point([other, tricky], name_of(tricky)).id == 1


# 9 ── re-layering a repository with nothing to choose never empties its tree

def test_relayer_with_nothing_chosen_keeps_what_is_served(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet)
    before = {p for p in (root / "example.com/a").rglob("*") if p.is_file() and "/@" not in p.as_posix()}
    with SiteDB(home) as db:
        db.set_meta(f"hold:{_key(cfg, 'a')}", "a layer that could not build")
        for t in db.tracks(_key(cfg, "a")):
            for p in db.points(t.id):
                db.stub_point(p.id)                  # nothing left to choose
    ops.relayer(home, "a")
    assert {p for p in (root / "example.com/a").rglob("*") if p.is_file() and "/@" not in p.as_posix()} == before
