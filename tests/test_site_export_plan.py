"""What a far side sends another network: per target, the points it hasn't
sent there (or since a date, or all), and only the files those need."""
import io

import pytest
from site_apt_rows import Repo

from mmw.site import home as H
from mmw.site.db import SiteDB
from mmw.site.export import export_blobs, plan_root_export
from mmw.site.roots import Roots
from mmw.site.timeline import import_rows

KEY = "https://ex.org/debian/"


def _day(db, store, day: int, *, skip_blob: str | None = None):
    """bookworm on `day`: p1.deb … p{day}.deb (each day adds one)."""
    r = Repo()
    pkgs = [r.deb(f"pool/p{i}.deb") for i in range(1, day + 1)]
    r.suite("bookworm", f"0{day} Nov 2026 00:00:00 UTC", pkgs)
    for cs, data in r.blobs.items():
        if not (skip_blob and r.rows.get(skip_blob) == cs):
            store.add_stream(io.BytesIO(data), expect=cs)
    import_rows(db, store, KEY, "debian", [r.base], r.rows, {c: len(b) for c, b in r.blobs.items()},
                f"2026110{day}T120000Z", None, set())
    return r


@pytest.fixture
def site(tmp_path):
    home = H.init_root(tmp_path / "srv", name="main", set_id="s1", network="oak")
    roots = Roots(H.load_site(home))
    store = roots.store(roots.primary)
    with SiteDB(home) as db:
        for d in (1, 2, 3):
            _day(db, store, d)
        db.set_meta("timeline", "1")
    return home, store


def _dates(plans):
    return [p.date[:8] for rp in plans for _t, p, _f in rp.points]


def test_new_skips_what_the_ledger_has(site):
    home, _store = site
    first = plan_root_export(home, "elm", repos=None, mode="new", since=None)
    assert sorted(_dates(first)) == ["20261101", "20261102", "20261103"]
    with SiteDB(home) as db:
        db.record_export("elm", [(KEY, p.ident) for rp in first for _t, p, _f in rp.points if p.date < "20261103"],
                         "20261115T000000Z")
        assert db.export_targets() == ["elm"]
    again = plan_root_export(home, "elm", repos=None, mode="new", since=None)
    assert _dates(again) == ["20261103"]
    assert sorted(_dates(plan_root_export(home, "pine", repos=None, mode="new", since=None))) == \
        ["20261101", "20261102", "20261103"]                          # per target


def test_new_blobs_skip_files_already_sent(site):
    home, _store = site
    plans = plan_root_export(home, "elm", repos=None, mode="new", since=None)
    with SiteDB(home) as db:
        db.record_export("elm", [(KEY, p.ident) for rp in plans for _t, p, _f in rp.points if p.date < "20261103"],
                         "20261115T000000Z")
    again = plan_root_export(home, "elm", repos=None, mode="new", since=None)
    blobs = export_blobs(home, "elm", again, "new")
    (_t, p3, files) = again[0].points[0]
    p1 = files["ex.org/debian/pool/p1.deb"]
    p3deb = files["ex.org/debian/pool/p3.deb"]
    assert p1 not in blobs and p3deb in blobs                       # p1.deb went with day 1
    assert export_blobs(home, "elm", again, "all")[p1] > 0


def test_since_and_all(site):
    home, _store = site
    assert _dates(plan_root_export(home, "elm", repos=None, mode="since", since="2026-11-02")) == ["20261103"]
    assert len(_dates(plan_root_export(home, "elm", repos=None, mode="all", since=None))) == 3


def test_incomplete_and_stub_points_are_skipped_and_said(site):
    home, store = site
    with SiteDB(home) as db:
        _day(db, store, 4, skip_blob="ex.org/debian/pool/p4.deb")    # its new package never arrived
        (t,) = db.tracks(KEY)
        oldest = db.points(t.id)[-1]
        db.stub_point(oldest.id)
    (rp,) = plan_root_export(home, "elm", repos=None, mode="all", since=None)
    assert sorted(p.date[:8] for _t, p, _f in rp.points) == ["20261102", "20261103"]
    assert any("2026-11-04" in s and "incomplete" in s for s in rp.skipped)
    assert any("2026-11-01" in s and "deleted" in s for s in rp.skipped)


def test_repos_filter_by_label(site):
    home, _store = site
    assert len(plan_root_export(home, "elm", repos={"debian"}, mode="all", since=None)) == 1
    assert plan_root_export(home, "elm", repos={"nothing-here"}, mode="all", since=None) == []
