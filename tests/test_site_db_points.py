"""Tracks, points (one upstream Release each) and states (one chosen point
per track, materialised) in the site database."""
import sqlite3

import pytest

from mmw.site.db import SiteDB
from mmw.site.tracks import PointDraft

T = "h/debian/dists/bookworm"


@pytest.fixture
def db(tmp_path):
    with SiteDB(tmp_path) as d:
        yield d


def _draft(ident="release:r1", date="20261101T000000Z", files=None, track=T):
    return PointDraft(track, ident, date, {"Suite": "bookworm"},
                      files if files is not None else {f"{T}/Release": "c1", "h/debian/pool/a.deb": "c2"})


SIZES = {"c1": 10, "c2": 20, "c3": 30, "c9": 90}


def _track(db):
    return db.ensure_track("R", T, {"Suite": "bookworm", "Codename": "bookworm"})


def test_import_point_is_idempotent(db):
    t = _track(db)
    pid, status = db.import_point(t, _draft(), SIZES, "20261102T000000Z", None)
    assert status == "new"
    assert db.import_point(t, _draft(), SIZES, "20261103T000000Z", None) == (pid, "same")
    assert db.point(pid).stamp == "20261102T000000Z"       # the first arrival is kept
    assert db.tracks("R")[0].suite == "bookworm"


def test_import_point_widens_and_detects_conflict(db):
    t = _track(db)
    pid, _ = db.import_point(t, _draft(), SIZES, "s", None)
    wider = _draft(files={f"{T}/Release": "c1", "h/debian/pool/a.deb": "c2", "h/debian/pool/b.deb": "c3"})
    assert db.import_point(t, wider, SIZES, "s", None) == (pid, "widened")
    assert set(db.point_files(pid)) == {f"{T}/Release", "h/debian/pool/a.deb", "h/debian/pool/b.deb"}
    clash = _draft(files={f"{T}/Release": "c1", "h/debian/pool/a.deb": "c9"})
    assert db.import_point(t, clash, SIZES, "s", None) == (pid, "conflict:h/debian/pool/a.deb")
    assert db.point_files(pid)["h/debian/pool/a.deb"] == "c2"


def test_importing_a_stub_refills_it(db):
    t = _track(db)
    pid, _ = db.import_point(t, _draft(), SIZES, "s", None)
    db.stub_point(pid)
    assert not db.point(pid).held and db.points(t) == [] and len(db.points(t, stubs=True)) == 1
    assert db.import_point(t, _draft(), SIZES, "s", None) == (pid, "refilled")
    assert db.point(pid).held


def test_points_order_by_date_then_arrival(db):
    t = _track(db)
    for ident, date in (("release:b", "20261102T000000Z"), ("release:c", "20261103T000000Z"),
                        ("release:a", "20261102T000000Z")):
        db.import_point(t, _draft(ident, date), SIZES, "s", None)
    assert [p.ident for p in db.points(t)] == ["release:c", "release:a", "release:b"]   # a came later


def test_state_lookup_by_points(db):
    t = _track(db)
    p1, _ = db.import_point(t, _draft(), SIZES, "s", None)
    t2 = db.ensure_track("R", "", {})
    p2, _ = db.import_point(t2, _draft("content:x", files={"h/debian/README": "c3"}, track=""), SIZES, "s", None)
    vid = db.add_state("R", "deb", ["h/debian"], [p2, p1], "dg", "s", None,
                       [(p, c, SIZES[c]) for p, c in {**db.point_files(p1), **db.point_files(p2)}.items()])
    assert db.state("R", [p1, p2]).id == vid
    assert db.state("R", [p1]) is None
    assert db.state_points(vid) == sorted([p1, p2])
    assert db.add_state("R", "deb", ["h/debian"], [p1, p2], "dg", "s", None, []) == vid


def test_stub_blobs_are_not_referenced(db):
    t = _track(db)
    pid, _ = db.import_point(t, _draft(), SIZES, "s", None)
    assert {"c1", "c2"} <= db.referenced_checksums()
    db.stub_point(pid)
    assert not {"c1", "c2"} & db.referenced_checksums()
    assert "s" in db.deleted_stamps("R")


def test_drop_unused_states_keeps_served_and_pending(db):
    t = _track(db)
    p1, _ = db.import_point(t, _draft(), SIZES, "s", None)
    p2, _ = db.import_point(t, _draft("release:r2", "20261105T000000Z"), SIZES, "s", None)
    p3, _ = db.import_point(t, _draft("release:r3", "20261106T000000Z"), SIZES, "s", None)
    served, pending, unused = (db.add_state("R", "deb", ["h/debian"], [p], f"d{p}", "s", None, [])
                               for p in (p1, p2, p3))
    db.set_serving("R", served, held=False)
    db.begin_switch("R", served, pending)
    assert db.drop_unused_states("R") == 1
    assert db.version_by_id(unused) is None and db.version_by_id(served) and db.version_by_id(pending)


def test_pin_and_previous(db):
    t = _track(db)
    pid, _ = db.import_point(t, _draft(), SIZES, "s", None)
    db.set_pin(t, pid)
    db.set_previous(t, pid)
    (info,) = db.tracks("R")
    assert info.pinned == pid and info.previous == pid
    db.set_pin(t, None)
    assert db.tracks("R")[0].pinned is None


def test_point_sizes(db):
    t = _track(db)
    p1, _ = db.import_point(t, _draft(), SIZES, "s", None)
    p2, _ = db.import_point(t, _draft("release:r2", "20261105T000000Z",
                                      files={f"{T}/Release": "c3", "h/debian/pool/a.deb": "c2"}), SIZES, "s", None)
    assert db.point_sizes(p1) == (30, 10)        # c2 is shared with p2
    assert db.point_sizes(p2) == (50, 30)


OLD_SCHEMA_VERSIONS = """
CREATE TABLE versions (
    id INTEGER PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL, label TEXT NOT NULL,
    digest TEXT NOT NULL, stamp TEXT NOT NULL, snap_label TEXT,
    UNIQUE (repo, number));
"""


def test_old_database_gains_tables(tmp_path):
    con = sqlite3.connect(tmp_path / "site.db")
    con.executescript(OLD_SCHEMA_VERSIONS)
    con.close()
    with SiteDB(tmp_path) as db:
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(versions)")}
        tables = {r[0] for r in db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "points" in cols and {"tracks", "points", "point_files"} <= tables
