"""Adopting APT trees mmw didn't make: found, grouped by upstream identity,
verified, and ingested as points on one repository's tracks."""
import os

from mmw.site.adopt import find_trees

from apt_tree import make_tree
from test_dry_run_totals import two_mirrors  # noqa: F401


def test_finds_archive_roots_below_a_path(tmp_path):
    make_tree(tmp_path / "www" / "debian", day=1)
    make_tree(tmp_path / "www" / "old" / "x" / "debian", day=2)
    trees = find_trees(tmp_path / "www")
    assert [t.path for t in trees] == [tmp_path / "www" / "debian", tmp_path / "www" / "old" / "x" / "debian"]
    t = trees[0].tracks[0]
    assert (t.dir, t.date, t.fields["Codename"]) == ("dists/bookworm", "20261101T000000Z", "bookworm")
    assert t.ident.startswith("release:") and not trees[0].flat


def test_flat_repository(tmp_path):
    make_tree(tmp_path / "flat", suite="tools", flat=True)
    (tree,) = find_trees(tmp_path)
    assert tree.flat and tree.tracks[0].dir == ""


def test_symlinks_out_of_the_tree_are_ignored(tmp_path):
    outside = make_tree(tmp_path / "outside", day=1)
    (tmp_path / "scan").mkdir()
    os.symlink(outside, tmp_path / "scan" / "link")
    assert find_trees(tmp_path / "scan") == []


def test_identity_groups_one_suite(tmp_path):
    a = make_tree(tmp_path / "a", day=1)
    b = make_tree(tmp_path / "b", day=2, packages={"hello_2.0_amd64.deb": b"two"})
    c = make_tree(tmp_path / "c", suite="trixie", day=3)
    ids = {t.path: t.identity() for t in find_trees(tmp_path)}
    assert ids[a] == ids[b] != ids[c]


# ── checking a tree ──────────────────────────────────────────────────────────

import pytest  # noqa: E402

from mmw.core.signing import Keyring  # noqa: E402
from mmw.site.adopt import check_tree, keyring_from  # noqa: E402


@pytest.fixture(scope="module")
def upstream(tmp_path_factory):
    """A signing key upstream used, and another one."""
    k = Keyring(tmp_path_factory.mktemp("upstream"))
    k.generate("up")
    k.generate("other")
    yield k
    k.stop_agent()


def _ring(upstream, tmp_path, name="up"):
    return keyring_from([upstream.public_armor(name).encode()], tmp_path / f"{name}.gpg")


@pytest.mark.parametrize("inrelease", [True, False])
def test_verified_signature(upstream, tmp_path, inrelease):
    make_tree(tmp_path / "t", sign_with=(upstream, "up"), inrelease=inrelease)
    (tree,) = find_trees(tmp_path / "t")
    check = check_tree(tree, _ring(upstream, tmp_path))
    assert check.signature == {"dists/bookworm": "verified"} and not check.refused


def test_bad_signature(upstream, tmp_path):
    make_tree(tmp_path / "t", sign_with=(upstream, "up"))
    (tree,) = find_trees(tmp_path / "t")
    check = check_tree(tree, _ring(upstream, tmp_path, "other"))
    assert check.signature["dists/bookworm"].startswith("bad")
    assert "dists/bookworm" in check.refused


def test_no_key_is_not_checked(upstream, tmp_path):
    make_tree(tmp_path / "t", sign_with=(upstream, "up"))
    (tree,) = find_trees(tmp_path / "t")
    check = check_tree(tree, None)
    assert check.signature == {"dists/bookworm": "not checked"} and not check.refused


def test_missing_and_mismatched_files(tmp_path):
    root = make_tree(tmp_path / "t", packages={"hello_1.0_amd64.deb": b"h" * 40, "gone_1.0_amd64.deb": b"g" * 9})
    (root / "pool/main/g/gone/gone_1.0_amd64.deb").unlink()
    (root / "pool/main/h/hello/hello_1.0_amd64.deb").write_bytes(b"damaged")
    (tree,) = find_trees(tmp_path / "t")
    check = check_tree(tree, None)
    assert check.missing == {"pool/main/g/gone/gone_1.0_amd64.deb": ["dists/bookworm"]}
    assert check.mismatched == {"pool/main/h/hello/hello_1.0_amd64.deb": ["dists/bookworm"]}
    assert "differs" in check.refused["dists/bookworm"]


def test_strays_by_kind(tmp_path):
    root = make_tree(tmp_path / "t", extra={
        "pool/main/o/old/old_0.1_amd64.deb": b"old",                       # no index names it: a stray
        "project/trace/master": b"trace",                                  # other
        "dists/bookworm/main/binary-amd64/by-hash/SHA256/" + "0" * 64: b"x",   # under a suite: kept
        "dists/bookworm/main/installer-amd64/current/images/MANIFEST": b"m",   # kept
        "README": b"read me", "archive-key.asc": b"KEY"})                  # top level: kept
    (tree,) = find_trees(root)
    check = check_tree(tree, None)
    assert check.strays == {"pool": ["pool/main/o/old/old_0.1_amd64.deb"], "other": ["project/trace/master"],
                            "damaged": ["dists/bookworm/main/binary-amd64/by-hash/SHA256/" + "0" * 64]}
    for kept in ("README", "archive-key.asc", "dists/bookworm/main/installer-amd64/current/images/MANIFEST"):
        assert kept in check.present


# ── adopting into a root ─────────────────────────────────────────────────────

from mmw.site import home as H  # noqa: E402
from mmw.site.adopt import adopt_tree  # noqa: E402
from mmw.site.db import SiteDB  # noqa: E402

BASE = "deb.debian.org/debian"


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "srv"
    return H.init_root(root, name="main", set_id="s1", network="oak"), root.resolve()


def _adopt(home, path, *, repo=None, keyring=None, base=BASE):
    (tree,) = find_trees(path)
    return adopt_tree(home, tree, check_tree(tree, keyring), repo=repo, label="Debian", base=base)


def _served_release(root, suite="bookworm"):
    return (root / BASE / "dists" / suite / "Release").read_bytes()


def test_adopting_a_tree_serves_it(site, tmp_path):
    home, root = site
    src = make_tree(tmp_path / "old", day=1)
    deb = src / "pool/main/h/hello/hello_1.0_amd64.deb"
    before = (deb.stat().st_ino, deb.stat().st_mtime_ns, deb.read_bytes())
    r = _adopt(home, src)
    served = root / BASE / "pool/main/h/hello/hello_1.0_amd64.deb"
    assert served.read_bytes() == before[2] and served.stat().st_ino == deb.stat().st_ino   # linked, not copied
    assert (deb.stat().st_ino, deb.stat().st_mtime_ns, deb.read_bytes()) == before         # untouched
    assert r.linked and not r.copied and not r.refused
    assert _served_release(root) == (src / "dists/bookworm/Release").read_bytes()


def test_three_trees_of_one_suite_are_three_points(site, tmp_path):
    home, root = site
    trees = [make_tree(tmp_path / f"t{d}", day=d, packages={f"hello_{d}.0_amd64.deb": bytes([d]) * 20})
             for d in (1, 2, 3)]
    for t in trees:
        r = _adopt(home, t)
    with SiteDB(home) as db:
        (track,) = [t for t in db.tracks(r.repo) if t.name.endswith("dists/bookworm")]
        assert len(db.points(track.id)) == 3
    assert _served_release(root) == (trees[2] / "dists/bookworm/Release").read_bytes()


def test_an_older_tree_does_not_move_serving(site, tmp_path):
    home, root = site
    new, old = make_tree(tmp_path / "new", day=3), make_tree(tmp_path / "old", day=1, packages={"x_1_amd64.deb": b"x"})
    _adopt(home, new)
    _adopt(home, old)
    assert _served_release(root) == (new / "dists/bookworm/Release").read_bytes()


def test_a_damaged_suite_alone_is_refused(site, tmp_path):
    home, root = site
    src = make_tree(tmp_path / "t", suite="bookworm", packages={"a_1_amd64.deb": b"a" * 9})
    make_tree(tmp_path / "t", suite="trixie", packages={"b_1_amd64.deb": b"b" * 9})
    (src / "pool/main/b/b/b_1_amd64.deb").write_bytes(b"damaged")
    r = _adopt(home, src)
    assert set(r.refused) == {"trixie"}
    assert _served_release(root, "bookworm") and not (root / BASE / "dists/trixie").exists()


def test_missing_files_store_it_incomplete(site, tmp_path):
    home, root = site
    src = make_tree(tmp_path / "t", packages={"a_1_amd64.deb": b"a" * 9, "b_1_amd64.deb": b"b" * 9})
    (src / "pool/main/b/b/b_1_amd64.deb").unlink()
    r = _adopt(home, src)
    assert r.incomplete == {"bookworm": 1}
    assert not (root / BASE / "dists/bookworm/Release").exists()          # nothing complete to serve


def test_unverified_points_are_marked(site, tmp_path):
    home, root = site
    r = _adopt(home, make_tree(tmp_path / "t"))
    assert r.unverified == ["bookworm"]
    with SiteDB(home) as db:
        (track,) = [t for t in db.tracks(r.repo) if t.name.endswith("dists/bookworm")]
        (p,) = db.points(track.id)
        from mmw.site.adopt import unverified_key
        assert db.get_meta(unverified_key(p)) == "1"


def test_verified_points_are_not_marked(site, tmp_path, upstream):
    home, root = site
    r = _adopt(home, make_tree(tmp_path / "t", sign_with=(upstream, "up")), keyring=_ring(upstream, tmp_path))
    assert r.unverified == []


def test_into_an_existing_repository(two_mirrors, tmp_path):  # noqa: F811
    from test_bundle_mmw import _mmw
    from test_site_apply_timeline import _apply, _bundle, set_a
    from test_snapshot_repo_versions import _key
    cfg, net, md = _mmw(two_mirrors)
    set_a(md, 2)
    home = H.init_root(tmp_path / "srv", name="main", set_id="s1", network=net.name)
    _apply(home, _bundle(cfg, net, md, tmp_path / "b1"))
    a = _key(cfg, "a")
    with SiteDB(home) as db:
        base = db.repo_state(a).bases[0]
        (track,) = [t for t in db.tracks(a) if t.name]
        serving = db.repo_state(a).serving.id
    old = make_tree(tmp_path / "old", suite="stable", day=1, flat=True, origin="ex",
                    packages={"old_1_all.deb": b"old"})
    r = _adopt(home, old, repo=a, base=base)
    assert r.repo == a and not r.refused
    with SiteDB(home) as db:
        assert len(db.points(track.id)) == 2 and db.repo_state(a).serving.id == serving
