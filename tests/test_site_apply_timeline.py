"""`mmw site apply` through timelines: bundles that arrive out of order, or
without what they start from, are stored — never refused — and each track
serves its newest complete point."""
import pytest

from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool
from mmw.core.publisher.bundle import bundle_network
from mmw.site import home as H
from mmw.site import ops
from mmw.site.apply import apply_bundle
from mmw.site.db import SiteDB

from test_bundle_mmw import _change_b, _mmw
from test_dry_run_totals import SHARED, _index, _sha
from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture
from test_snapshot_repo_versions import _key

DATES = {1: "Fri, 01 Nov 2026 00:00:00 UTC", 2: "Sat, 02 Nov 2026 00:00:00 UTC",
         3: "Sun, 03 Nov 2026 00:00:00 UTC", 4: "Mon, 04 Nov 2026 00:00:00 UTC",
         5: "Tue, 05 Nov 2026 00:00:00 UTC", 6: "Wed, 06 Nov 2026 00:00:00 UTC"}


def set_a(mirror_dir, day: int) -> None:
    """Mirror `a` as upstream published it on `day`: a.deb … a{day}.deb, a
    Packages and a Release dated that day."""
    files = {"shared.deb": SHARED, "a.deb": b"a" * 300}
    files.update({f"a{i}.deb": f"A{i}".encode() * 40 for i in range(2, day + 1)})
    with Pool(mirror_dir) as pool:
        refs = []
        for fname, data in files.items():
            pool.add(data, _sha(data), "sha256", fname, len(data))
            refs.append((_sha(data), fname, KIND_PKG))
        idx = _index(files)
        pool.add(idx, _sha(idx), "sha256", "Packages", len(idx))
        refs.append((_sha(idx), "Packages", KIND_META))
        rel = (f"Origin: ex\nSuite: stable\nDate: {DATES[day]}\nSHA256:\n"
               f" {_sha(idx)} {len(idx)} Packages\n").encode()
        pool.add(rel, _sha(rel), "sha256", "Release", len(rel))
        refs.append((_sha(rel), "Release", KIND_RELEASE))
        pool.reconcile_refs("a", refs)


@pytest.fixture
def env(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = _mmw(two_mirrors)
    set_a(md, 1)
    home = H.init_root(tmp_path / "srv", name="main", set_id="s1", network=net.name)
    return cfg, net, md, home


def _bundle(cfg, net, md, out, **kw):
    return str(bundle_network(net, cfg, md, out_path=out, **kw).out_path)


def _apply(home, path, **kw):
    return apply_bundle(home, path, say=lambda *a: None, **kw)


def _track(db, repo):
    (t,) = [t for t in db.tracks(repo) if t.name]
    return t


def _served_day(db, repo) -> str:
    t = _track(db, repo)
    pts = set(db.state_points(db.repo_state(repo).serving.id))
    return next(db.point(p).date[:8] for p in pts if db.point(p).track == t.id)


def test_older_bundle_joins_the_timeline(env, tmp_path):
    cfg, net, md, home = env
    a = _key(cfg, "a")
    b1 = _bundle(cfg, net, md, tmp_path / "1", from_ref=None)
    set_a(md, 2)
    b2 = _bundle(cfg, net, md, tmp_path / "2", from_ref=None)
    _apply(home, b2)
    r = _apply(home, b1)
    with SiteDB(home) as db:
        assert [p.date[:8] for p in db.points(_track(db, a).id)] == ["20261102", "20261101"]
        assert _served_day(db, a) == "20261102"
    assert any("2026-11-01 stored (older than the 2026-11-02 being served)" in ln for ln in r.lines)
    assert not r.refused


def test_delta_without_its_start_stores_incomplete_points(env, tmp_path):
    cfg, net, md, home = env
    a, b = _key(cfg, "a"), _key(cfg, "b")
    _apply(home, _bundle(cfg, net, md, tmp_path / "f", from_ref=None))
    set_a(md, 2)
    _bundle(cfg, net, md, tmp_path / "d1")                # never reaches the site
    set_a(md, 3)
    _change_b(md)
    r = _apply(home, _bundle(cfg, net, md, tmp_path / "d2"))
    assert b in r.updated and a not in r.updated and not r.refused
    assert [o.missing for o in r.incomplete[a]] == [1]
    assert any("stable 2026-11-03 stored, incomplete: 1 file(s) missing" in ln for ln in r.lines)
    with SiteDB(home) as db:
        assert _served_day(db, a) == "20261101"
    r = _apply(home, _bundle(cfg, net, md, tmp_path / "full", from_ref=None))
    assert a in r.updated
    with SiteDB(home) as db:
        assert _served_day(db, a) == "20261103"


def test_conflicting_point_is_refused_alone(env, tmp_path):
    cfg, net, md, home = env
    a, b = _key(cfg, "a"), _key(cfg, "b")
    full = _bundle(cfg, net, md, tmp_path / "f", from_ref=None)
    _apply(home, full)
    with SiteDB(home) as db:                              # this mirror's record of a.deb goes wrong
        pid = db.points(_track(db, a).id)[0].id
        bad = db._blob_id("f" * 64, 1)
        db._conn.execute("UPDATE point_files SET blob = ? WHERE point = ? AND path = "
                         "(SELECT id FROM paths WHERE path LIKE '%/a.deb')", (bad, pid))
        db.commit()
    _change_b(md)
    r = _apply(home, _bundle(cfg, net, md, tmp_path / "f2", from_ref=None))
    assert a in r.refused and b in r.updated
    assert any("refused:" in ln and "a.deb differs inside one Release" in ln for ln in r.lines)


def test_held_repo_stores_without_switching(env, tmp_path):
    cfg, net, md, home = env
    a = _key(cfg, "a")
    _apply(home, _bundle(cfg, net, md, tmp_path / "f", from_ref=None))
    ops.hold(home, a)
    set_a(md, 2)
    r = _apply(home, _bundle(cfg, net, md, tmp_path / "d"))
    assert a in r.stored_held
    with SiteDB(home) as db:
        assert _served_day(db, a) == "20261101"
        assert [p.date[:8] for p in db.points(_track(db, a).id)] == ["20261102", "20261101"]


def _add_c(cfg, md):
    """A third repository, only network elm mirrors."""
    from mmw.core import config
    data = b"c" * 120
    idx = _index({"c.deb": data})
    with Pool(md) as pool:
        pool.add(data, _sha(data), "sha256", "c.deb", len(data))
        pool.add(idx, _sha(idx), "sha256", "Packages", len(idx))
        pool.reconcile_refs("c", [(_sha(data), "c.deb", KIND_PKG), (_sha(idx), "Packages", KIND_META)])
    c = config.SourceConfig(name="c", label="c", provider="apt-flat", url="https://example.net/c/")
    return cfg.model_copy(update={"sources": [*cfg.sources, c]})


def test_one_conflicting_suite_never_refuses_the_whole_repository(env, tmp_path):
    """`old` conflicts, `stable` is fine: the repository isn't refused — the
    suite is, and says so."""
    from test_bundle_ranges_review import _with_old_suite
    cfg, net, md, home = env
    a = _key(cfg, "a")
    _with_old_suite(md, True)
    _apply(home, _bundle(cfg, net, md, tmp_path / "f", from_ref=None))
    with SiteDB(home) as db:                              # this mirror's record of old's index goes wrong
        old = next(t for t in db.tracks(a) if t.name.endswith("/old"))
        pid = db.points(old.id)[0].id
        bad = db._blob_id("f" * 64, 1)
        db._conn.execute("UPDATE point_files SET blob = ? WHERE point = ? AND path = "
                         "(SELECT id FROM paths WHERE path LIKE '%/old/Packages')", (bad, pid))
        db.commit()
    r = _apply(home, _bundle(cfg, net, md, tmp_path / "f2", from_ref=None))
    assert a not in r.refused
    assert [s for s in r.refused_suites[a]] and all("old" in s for s in r.refused_suites[a])
    assert any("refused:" in ln and "old" in ln for ln in r.lines)


def test_the_review_compares_upstream_dates_with_upstream_dates(env, tmp_path):
    """Serving 1 Nov (stored today), the bundle brings 2 Nov: the review reads
    1 Nov → 2 Nov, never today → 2 Nov."""
    from mmw.site.apply import plan_apply
    cfg, net, md, home = env
    a = _key(cfg, "a")
    _apply(home, _bundle(cfg, net, md, tmp_path / "1", from_ref=None))
    set_a(md, 2)
    plan = plan_apply(home, _bundle(cfg, net, md, tmp_path / "2"))
    (row,) = [r for r in plan.rows if r.repo == a]
    assert row.serving_date[:8] == "20261101" and row.to_date[:8] == "20261102"


def test_serving_a_bundle_says_a_suite_whose_point_is_gone(env, tmp_path):
    """`stable` rolls back; `old`'s point from then was deleted — said, not silent."""
    from mmw.site.mset import open_set
    from test_bundle_ranges_review import _with_old_suite
    cfg, net, md, home = env
    a = _key(cfg, "a")
    _with_old_suite(md, True)
    first = _apply(home, _bundle(cfg, net, md, tmp_path / "1", from_ref=None)).created
    ops.delete_point(home, a, "2026-11-01", track="old", force=True)
    steps = ops.bundle_plan(open_set([str(tmp_path / "srv")]), first)
    (step,) = [s for s in steps if s.key == a]
    assert step.target and step.note and "old" in step.note and "deleted" in step.note
