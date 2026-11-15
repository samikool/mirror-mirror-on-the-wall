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


# ── a bundle from another network ────────────────────────────────────────────

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


@pytest.fixture
def two_nets(env, tmp_path):
    from mmw.site.recents import remember
    cfg, net, md, _home = env
    elm = net.model_copy(update={"name": "elm", "root": tmp_path / "pub-elm"})
    remember  # noqa: B018 — imported so XDG state is per test (below)
    return cfg, net, elm, md


def _set_apply(root, path, **kw):
    from mmw.site.apply import apply_to_set
    from mmw.site.mset import open_set
    return apply_to_set(open_set([str(root)]), path, say=lambda *a: None, **kw)


@pytest.fixture(autouse=True)
def _xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _oak_site(cfg, net, md, tmp_path, days=(1, 2, 4)):
    from mmw.site.apply import apply_to_set
    root = tmp_path / "oak-srv"
    for i, day in enumerate(days):
        set_a(md, day)
        b = _bundle(cfg, net, md, tmp_path / f"x{day}", from_ref=None)
        if i == 0:
            apply_to_set(None, b, place={"*": str(root)}, say=lambda *a: None)
        else:
            _set_apply(root, b)
    return root


def _root_db(root):
    from mmw.site.home import read_root
    return SiteDB(read_root(str(root)).home)


def _nginx(root) -> str:
    return "".join(p.read_text() for p in sorted((root / "nginx").rglob("*.conf")))


def test_other_networks_history_merges(two_nets, tmp_path):
    cfg, oak, elm, md = two_nets
    root = _oak_site(cfg, oak, md, tmp_path)
    before = _nginx(root)
    set_a(md, 3)
    r = _set_apply(root, _bundle(cfg.model_copy(update={"networks": [oak, elm]}), elm, md,
                                 tmp_path / "y3", from_ref=None))
    assert r.foreign
    assert r.lines[0] == ("this bundle is for network 'elm' — this mirror holds 'oak': importing repository "
                          "history; its network files (nginx, settings, TLS) are not applied")
    with _root_db(root) as db:
        a = _key(cfg, "a")
        assert [p.date[:8] for p in db.points(_track(db, a).id)] == ["20261104", "20261103", "20261102",
                                                                      "20261101"]
        assert _served_day(db, a) == "20261104"
    assert _nginx(root) == before


def test_foreign_repo_skipped_by_default(two_nets, tmp_path):
    cfg, oak, elm, md = two_nets
    root = _oak_site(cfg, oak, md, tmp_path, days=(1,))
    cfg2 = _add_c(cfg, md).model_copy(update={"networks": [oak, elm]})
    r = _set_apply(root, _bundle(cfg2, elm, md, tmp_path / "y", from_ref=None))
    assert r.skipped == ["c"]
    assert any("c  not mirrored here — skipped (deploy it: mmw site apply BUNDLE --deploy 'c')" in ln
               for ln in r.lines)
    assert not (root / "example.net").exists()
    with _root_db(root) as db:
        assert "https://example.net/c/" not in {s.repo for s in db.repo_states()}


def test_foreign_repo_deployed_on_request(two_nets, tmp_path):
    cfg, oak, elm, md = two_nets
    root = _oak_site(cfg, oak, md, tmp_path, days=(1,))
    cfg2 = _add_c(cfg, md).model_copy(update={"networks": [oak, elm]})
    r = _set_apply(root, _bundle(cfg2, elm, md, tmp_path / "y", from_ref=None), deploy={"c"},
                   place={"example.net": str(root)})
    assert r.skipped == []
    assert (root / "example.net/c/c.deb").read_bytes() == b"c" * 120
    assert "example.net" in _nginx(root)
    # The site's own network keeps c's server block on its next bundle.
    set_a(md, 2)
    _set_apply(root, _bundle(cfg, oak, md, tmp_path / "x2", from_ref=None))
    assert "example.net" in _nginx(root) and (root / "example.net/c/c.deb").exists()
