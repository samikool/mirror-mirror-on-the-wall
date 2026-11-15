"""A far side applies a bundle of timeline points (format 3): each point is
checked and joins its track; network files never apply; no suite retires."""
import io

import pytest
from site_apt_rows import Repo
from site_format3 import write_format3

from mmw.site import home as H
from mmw.site.apply import apply_bundle
from mmw.site.db import SiteDB
from mmw.site.roots import Roots
from mmw.site.switch import switch_in
from mmw.site.timeline import choose, ensure_state, import_rows
from mmw.site.tracks import split_tracks

from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply_timeline import _apply, _bundle, _served_day, _track, env, set_a  # noqa: F401
from test_snapshot_repo_versions import _key

DAYS = {d: f"{d:02d} Nov 2026 00:00:00 UTC" for d in range(1, 10)}


def flat_point(day: int, base="example.com/a"):
    """A flat repository's Release on `day`, as a draft plus its blobs."""
    r = Repo(base)
    r.deb(f"x{day}.deb")
    r.suite("stable", DAYS[day], [f"x{day}.deb"], flat=True, compress="")
    (d,) = split_tracks(r.rows, [base], r.read, f"202611{day:02d}T000000Z")
    return d, r.blobs


def _f3(tmp_path, repos, blobs, **kw):
    return write_format3(tmp_path / "p.tar", "elm", repos, blobs, **kw)


def test_format3_points_join_the_timeline(env, tmp_path):  # noqa: F811
    cfg, net, md, home = env
    a = _key(cfg, "a")
    for d in (1, 3):
        set_a(md, d)
        _apply(home, _bundle(cfg, net, md, tmp_path / f"b{d}", from_ref=None))
    points, blobs = [], {}
    for d in (2, 4):
        draft, b = flat_point(d)
        points.append((draft, f"202611{d:02d}T120000Z", None))
        blobs.update(b)
    r = apply_bundle(home, _f3(tmp_path, {a: ("a", ["example.com/a"], points)}, blobs), say=lambda *x: None)
    assert a in r.updated
    with SiteDB(home) as db:
        assert [p.date[:8] for p in db.points(_track(db, a).id)] == ["20261104", "20261103", "20261102",
                                                                      "20261101"]
        assert _served_day(db, a) == "20261104"


def test_a_point_whose_release_does_not_match_is_refused_alone(env, tmp_path):  # noqa: F811
    cfg, net, md, home = env
    a, b = _key(cfg, "a"), _key(cfg, "b")
    _apply(home, _bundle(cfg, net, md, tmp_path / "b1", from_ref=None))
    liar, blobs = flat_point(2)
    rel = "example.com/a/Release"
    forged = blobs[liar.files[rel]] + b"Extra: forged\n"
    import hashlib
    cs = hashlib.sha256(forged).hexdigest()
    blobs[cs] = forged
    liar = liar.__class__(liar.track, liar.ident, liar.date, liar.fields, {**liar.files, rel: cs})
    other, more = flat_point(3, base="example.com/b")
    other = other.__class__("example.com/b", other.ident, other.date, other.fields, other.files)
    blobs.update(more)
    r = apply_bundle(home, _f3(tmp_path, {a: ("a", ["example.com/a"], [(liar, "20261102T1200Z", None)]),
                                          b: ("b", ["example.com/b"], [(other, "20261103T1200Z", None)])},
                               blobs), say=lambda *x: None)
    assert a in r.refused and "does not match" in r.refused[a]
    assert b in r.updated


@pytest.fixture
def two_suites(tmp_path):
    home = H.init_root(tmp_path / "srv", name="main", set_id="s1", network="oak")
    roots = Roots(H.load_site(home))
    store = roots.store(roots.primary)
    r = Repo()
    r.suite("old", DAYS[1], [])
    r.suite("new", DAYS[1], [])
    with SiteDB(home) as db:
        for cs, data in r.blobs.items():
            store.add_stream(io.BytesIO(data), expect=cs)
        import_rows(db, store, "K", "debian", [r.base], r.rows, {c: len(b) for c, b in r.blobs.items()},
                    "20261101T120000Z", None, set())
        vid, _w = ensure_state(db, store, "K", choose(db, store, "K")[0])
        switch_in(db, roots, "K", vid, held=False)
    return home, tmp_path / "srv"


def test_format3_never_retires_a_suite(two_suites, tmp_path):
    home, root = two_suites
    r = Repo()
    r.suite("new", DAYS[2], [])
    (draft,) = split_tracks(r.rows, [r.base], r.read, "20261102T000000Z")
    apply_bundle(home, _f3(tmp_path, {"K": ("debian", [r.base], [(draft, "20261102T120000Z", None)])},
                           r.blobs), say=lambda *x: None)
    assert (root / "ex.org/debian/dists/old/Release").exists()
    assert "02 Nov" in (root / "ex.org/debian/dists/new/Release").read_text()


def test_format3_network_files_are_never_applied(two_suites, tmp_path):
    home, root = two_suites
    r = Repo()
    r.suite("new", DAYS[2], [])
    (draft,) = split_tracks(r.rows, [r.base], r.read, "20261102T000000Z")
    apply_bundle(home, _f3(tmp_path, {"K": ("debian", [r.base], [(draft, "20261102T120000Z", None)])},
                           r.blobs, network_files={"nginx/x.conf": b"server {}"}), say=lambda *x: None)
    assert not (root / "nginx/x.conf").exists()


# ── through a set of roots (the CLI and TUI path) ────────────────────────────

@pytest.fixture
def oak_set(env, tmp_path, monkeypatch):  # noqa: F811
    from mmw.site.apply import apply_to_set
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg, net, md, _home = env
    set_a(md, 1)
    root = tmp_path / "r1"
    apply_to_set(None, _bundle(cfg, net, md, tmp_path / "b1", from_ref=None), place={"*": str(root)},
                 say=lambda *x: None)
    return cfg, root


def _c_mirror():
    from mmw.core.config import MirrorConfig
    from mmw.core.repos import repository_key
    m = MirrorConfig(name="c", provider="apt-flat", source="https://example.net/c", source_url="https://example.net/c/",
                     label="c")
    return repository_key(m), m.model_dump(mode="json")


def test_format3_through_a_set(oak_set, tmp_path):
    from mmw.site.apply import apply_to_set
    from mmw.site.home import read_root
    from mmw.site.mset import open_set
    cfg, root = oak_set
    a = _key(cfg, "a")
    draft, blobs = flat_point(2)
    r = apply_to_set(open_set([str(root)]), _f3(tmp_path, {a: ("a", ["example.com/a"],
                                                                [(draft, "20261102T1200Z", None)])}, blobs),
                     say=lambda *x: None)
    assert r.foreign and a in r.merged().updated
    with SiteDB(read_root(str(root)).home) as db:
        assert _served_day(db, a) == "20261102"


def test_deploy_from_a_format3_bundle(oak_set, tmp_path):
    import json
    from mmw.site.apply import apply_to_set
    from mmw.site.mset import open_set
    cfg, root = oak_set
    key, mirror = _c_mirror()
    draft, blobs = flat_point(5, base="example.net/c")
    settings = json.dumps({"network": {"name": "elm"}, "mirrors": [mirror]})
    path = _f3(tmp_path, {key: ("c", ["example.net/c"], [(draft, "20261105T1200Z", None)])}, blobs,
               network_files={"keys/c/c.asc": b"KEY"}, settings=settings)
    r = apply_to_set(open_set([str(root)]), path, say=lambda *x: None)
    assert r.skipped == ["c"] and not (root / "example.net").exists()
    r = apply_to_set(open_set([str(root)]), path, deploy={"c"}, place={"example.net": str(root)},
                     say=lambda *x: None)
    assert r.skipped == [] and (root / "example.net/c/x5.deb").exists()
    assert (root / "keys/c/c.asc").read_bytes() == b"KEY"
    assert "example.net" in "".join(p.read_text() for p in (root / "nginx").rglob("*.conf"))


def test_review_counts_points(env, tmp_path):  # noqa: F811
    from mmw.site.apply import plan_apply
    from mmw.tui.site.apply_flow import _row_text
    cfg, net, md, home = env
    a = _key(cfg, "a")
    _apply(home, _bundle(cfg, net, md, tmp_path / "b1", from_ref=None))
    points, blobs = [], {}
    for d in (2, 3):
        draft, b = flat_point(d)
        points.append((draft, f"202611{d:02d}T120000Z", None))
        blobs.update(b)
    plan = plan_apply(home, _f3(tmp_path, {a: ("a", ["example.com/a"], points)}, blobs))
    (row,) = plan.rows
    assert row.points == 2 and not row.unchanged and row.files_sent == len(blobs) and row.bytes_sent > 0
    assert _row_text(row, "/srv").startswith("a  +2 point(s)")
