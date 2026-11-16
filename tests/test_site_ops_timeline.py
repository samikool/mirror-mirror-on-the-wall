"""Operating a timeline: serve (pin) a point, forward, delete (stub) one,
retention, history — what is protected and why."""
import pytest

from mmw.site import ops
from mmw.site.db import SiteDB

from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture
from test_site_apply_timeline import _apply, _bundle, _served_day, _track, env, set_a  # noqa: F401
from test_snapshot_repo_versions import _key


def _days(env, tmp_path, days):  # noqa: F811
    cfg, net, md, home = env
    for d in days:
        set_a(md, d)
        _apply(home, _bundle(cfg, net, md, tmp_path / f"b{d}", from_ref=None))
    return cfg, net, md, home, _key(cfg, "a")


def _marks(home, a):
    ((track, rows),) = [(t, r) for t, r in ops.history(home, a) if t.name]
    return {p.date[:8]: mark for p, _total, _frees, mark in rows}


def test_serve_pins_the_track_and_apply_keeps_it(env, tmp_path):  # noqa: F811
    cfg, net, md, home, a = _days(env, tmp_path, (1, 2))
    ops.serve(home, a, "2026-11-01")
    with SiteDB(home) as db:
        assert _served_day(db, a) == "20261101" and _track(db, a).pinned is not None
    set_a(md, 3)
    _apply(home, _bundle(cfg, net, md, tmp_path / "b3", from_ref=None))
    with SiteDB(home) as db:
        assert _served_day(db, a) == "20261101"
    assert _marks(home, a) == {"20261103": "", "20261102": "previous", "20261101": "serving"}


def test_serve_as_of_a_date_takes_the_newest_then(env, tmp_path):  # noqa: F811
    _cfg, _net, _md, home, a = _days(env, tmp_path, (1, 3))
    ops.serve(home, a, "2026-11-02")
    with SiteDB(home) as db:
        assert _served_day(db, a) == "20261101"


def test_forward_clears_pins(env, tmp_path):  # noqa: F811
    cfg, net, md, home, a = _days(env, tmp_path, (1, 2))
    ops.serve(home, a, "2026-11-01")
    assert ops.forward(home, a) == [a]
    with SiteDB(home) as db:
        assert _served_day(db, a) == "20261102" and _track(db, a).pinned is None


def test_delete_stubs_and_frees(env, tmp_path):  # noqa: F811
    _cfg, _net, _md, home, a = _days(env, tmp_path, (1, 2, 3))
    assert ops.delete_point(home, a, "2026-11-01") > 0
    assert _marks(home, a)["20261101"] == "stub"
    with SiteDB(home) as db:
        assert len(db.points(_track(db, a).id)) == 2


def test_delete_previous_refuses_with_reason(env, tmp_path):  # noqa: F811
    _cfg, _net, _md, home, a = _days(env, tmp_path, (1, 2))
    with pytest.raises(ValueError, match="served before"):
        ops.delete_point(home, a, "2026-11-01")


def test_delete_served_needs_force(env, tmp_path):  # noqa: F811
    _cfg, _net, _md, home, a = _days(env, tmp_path, (1, 2))
    with pytest.raises(ValueError, match="being served") as e:
        ops.delete_point(home, a, "2026-11-02")
    assert "2026-11-01" in str(e.value)                     # what serves next
    ops.delete_point(home, a, "2026-11-02", force=True)
    with SiteDB(home) as db:
        assert _served_day(db, a) == "20261101"


def test_retention_keeps_newest_n_plus_protected(env, tmp_path):  # noqa: F811
    _cfg, _net, _md, home, a = _days(env, tmp_path, (1, 2, 3, 4))
    from mmw.site.roots import Roots
    from mmw.site.home import load_site
    with SiteDB(home) as db:
        gone = ops.apply_retention(db, Roots(load_site(home)), load_site(home).model_copy(update={"keep": 1}))
    assert sorted(d for _r, _s, d in gone) == ["2026-11-01", "2026-11-02"]
    assert _marks(home, a) == {"20261104": "serving", "20261103": "previous", "20261102": "stub",
                               "20261101": "stub"}


def test_delete_ambiguous_across_suites_asks_for_the_track(env, tmp_path):  # noqa: F811
    _cfg, _net, _md, home, a = _days(env, tmp_path, (1,))
    with pytest.raises(LookupError, match="no suite"):
        ops.delete_point(home, a, "2026-11-01", track="nonesuch")


def test_force_deleting_the_only_point_stops_serving_it(env, tmp_path):  # noqa: F811
    """`… then serves nothing` means it: the repository's files leave the root."""
    _cfg, _net, _md, home, a = _days(env, tmp_path, (1,))
    served = tmp_path / "srv" / "example.com" / "a"

    def files():
        return [p for p in served.rglob("*") if p.is_file()] if served.exists() else []
    assert files()
    with pytest.raises(ValueError, match="serves nothing"):
        ops.delete_point(home, a, "2026-11-01")
    ops.delete_point(home, a, "2026-11-01", force=True)
    assert files() == []


def test_a_serve_resumed_after_a_kill_keeps_the_repository_unheld(env, tmp_path, monkeypatch):  # noqa: F811
    """Serving an older day pins its suite; it never holds the repository.
    Killed halfway and finished later, the same — not decided by v-numbers."""
    from mmw.site.lock import break_lock
    from mmw.site.store import Store
    from mmw.site.switch import resume_switches
    from mmw.site.roots import Roots
    from mmw.site.home import load_site
    _cfg, _net, _md, home, a = _days(env, tmp_path, (1, 2))
    real = Store.place

    def boom(self, cs, dest):
        if dest.name == "Packages":
            raise KeyboardInterrupt
        return real(self, cs, dest)
    monkeypatch.setattr(Store, "place", boom)
    with pytest.raises(KeyboardInterrupt):
        ops.serve(home, a, "2026-11-01")
    monkeypatch.setattr(Store, "place", real)
    break_lock(home)
    with SiteDB(home) as db:
        assert resume_switches(db, Roots(load_site(home))) == [a]
        assert not db.repo_state(a).held and _served_day(db, a) == "20261101"


def test_point_sizes_looks_each_blob_up_rather_than_listing_every_file(env, tmp_path):  # noqa: F811
    """History asks this per point: no full scan of the other points' or
    states' files."""
    _cfg, _net, _md, home, a = _days(env, tmp_path, (1, 2))
    with SiteDB(home) as db:
        pid = db.points(_track(db, a).id)[0].id
        total, only = db.point_sizes(pid)
        assert total > 0 and 0 <= only <= total
        captured = []
        db._conn.set_trace_callback(captured.append)
        db.point_sizes(pid)
        db._conn.set_trace_callback(None)
        only_sql = captured[-1]                       # traced with its values filled in
        plan = " ".join(r[-1] for r in db._conn.execute("EXPLAIN QUERY PLAN " + only_sql).fetchall())
        assert "SCAN version_files" not in plan and "SCAN f " not in plan and "SCAN point_files" not in plan, plan


def test_a_resumed_switch_finishes_with_the_hold_it_began_with(env, tmp_path, monkeypatch):  # noqa: F811
    """Held (its layer couldn't be built), then a fast-forward killed halfway:
    resumed, it ends as begun — not held — never held with no reason given."""
    from mmw.site.home import load_site
    from mmw.site.lock import break_lock
    from mmw.site.roots import Roots
    from mmw.site.store import Store
    from mmw.site.switch import resume_switches, switch_in
    _cfg, _net, _md, home, a = _days(env, tmp_path, (1, 2))
    from mmw.site.timeline import ensure_state
    with SiteDB(home) as db:
        db.set_held(a, True)
        t = _track(db, a)
        roots = Roots(load_site(home))
        older_id, _w = ensure_state(db, roots.store(roots.primary), a, {t.id: db.points(t.id)[1].id})
    real = Store.place

    def boom(self, cs, dest):
        if dest.name == "Packages":
            raise KeyboardInterrupt
        return real(self, cs, dest)
    monkeypatch.setattr(Store, "place", boom)
    with SiteDB(home) as db, pytest.raises(KeyboardInterrupt):
        switch_in(db, Roots(load_site(home)), a, older_id, held=False)
    monkeypatch.setattr(Store, "place", real)
    break_lock(home)
    with SiteDB(home) as db:
        assert resume_switches(db, Roots(load_site(home))) == [a]
        assert not db.repo_state(a).held


def test_a_forced_delete_never_unserves_when_older_points_lost_files(env, tmp_path):  # noqa: F811
    """Serving 2 Nov; 1 Nov is kept but its files are gone here. Deleting 2 Nov
    with --yes keeps what is served rather than serving nothing."""
    from mmw.site.home import load_site
    from mmw.site.roots import Roots
    _cfg, _net, _md, home, a = _days(env, tmp_path, (1, 2))
    roots = Roots(load_site(home))
    with SiteDB(home) as db:
        t = _track(db, a)
        older, newer = db.points(t.id)[1], db.points(t.id)[0]
        only_older = set(db.point_files(older.id).values()) - set(db.point_files(newer.id).values())
    store = roots.store(roots.primary)
    for cs in only_older:
        store.path(cs).unlink()
    served = tmp_path / "srv" / "example.com" / "a"

    def live():                                   # what is served, not the dated trees beside it
        return sorted(str(p.relative_to(served)) for p in served.rglob("*")
                      if p.is_file() and not str(p.relative_to(served)).startswith("@"))
    before = live()
    ops.delete_point(home, a, "2026-11-02", force=True)
    assert live() == before and before


def test_a_clean_up_keeps_what_is_known_complete(env, tmp_path):  # noqa: F811
    """A GC frees only what no held point holds: the others needn't be checked again."""
    _cfg, _net, _md, home, a = _days(env, tmp_path, (1, 2, 3))
    with SiteDB(home) as db:
        t = _track(db, a)
        for p in db.points(t.id):
            db.set_complete(p.id, True)
    ops.delete_point(home, a, "2026-11-01")                        # its files go: a GC runs
    with SiteDB(home) as db:
        assert all(db.known_complete(p.id) for p in db.points(t.id))


def test_deleting_and_cleaning_say_what_they_are_doing(env, tmp_path):  # noqa: F811
    """A run page hears the phases, and each file the GC removes."""
    _cfg, _net, _md, home, a = _days(env, tmp_path, (1, 2, 3, 4))
    heard = []
    ops.delete_point(home, a, "2026-11-01", progress=lambda event, **data: heard.append((event, data)))
    phases = [d["text"] for e, d in heard if e == "phase"]
    assert any("freeing space" in p for p in phases) and any("dated trees" in p for p in phases)
    assert any(e == "receive" for e, _d in heard)                      # a removed file, named
    heard.clear()
    from mmw.site.home import load_site, save_settings
    save_settings(home, load_site(home).model_copy(update={"keep": 1}))
    ops.clean(home, progress=lambda event, **data: heard.append((event, data)))
    assert any(e == "phase" and "deleting" in d["text"] for e, d in heard)
