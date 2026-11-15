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
        gone = ops.apply_retention(db, Roots(load_site(home)), 1)
    assert sorted(d for _r, _s, d in gone) == ["2026-11-01", "2026-11-02"]
    assert _marks(home, a) == {"20261104": "serving", "20261103": "previous", "20261102": "stub",
                               "20261101": "stub"}


def test_delete_ambiguous_across_suites_asks_for_the_track(env, tmp_path):  # noqa: F811
    _cfg, _net, _md, home, a = _days(env, tmp_path, (1,))
    with pytest.raises(LookupError, match="no suite"):
        ops.delete_point(home, a, "2026-11-01", track="nonesuch")
