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


# ── checking signatures later ────────────────────────────────────────────────

def test_a_key_added_later_clears_the_mark(site, tmp_path, upstream):
    from mmw.site.adopt import recheck_signatures
    from mmw.site.ops import unverified
    home, _root = site
    r = _adopt(home, make_tree(tmp_path / "t", sign_with=(upstream, "up")))          # no key yet
    assert unverified(home, r.repo)
    verdicts = recheck_signatures(home, _ring(upstream, tmp_path))
    assert [(v[1].split()[0], v[2]) for v in verdicts] == [("stable", "verified")]
    assert unverified(home, r.repo) == []


def test_a_wrong_key_leaves_it(site, tmp_path, upstream):
    from mmw.site.adopt import recheck_signatures
    from mmw.site.ops import unverified
    home, _root = site
    r = _adopt(home, make_tree(tmp_path / "t", sign_with=(upstream, "up"), inrelease=False))
    (v,) = recheck_signatures(home, _ring(upstream, tmp_path, "other"))
    assert v[2].startswith("bad") and unverified(home, r.repo)


def test_an_index_entry_with_md5_only_is_said(tmp_path):
    """An entry with no SHA256 has nothing to check its file by: counted and
    said, not silently left out."""
    import hashlib
    root = make_tree(tmp_path / "t", packages={"hello_1.0_amd64.deb": b"h" * 40, "old_1.0_amd64.deb": b"o" * 9})
    ix = root / "dists/bookworm/main/binary-amd64/Packages"
    text = ix.read_text()
    old_sha = hashlib.sha256(b"o" * 9).hexdigest()
    new = text.replace(f"SHA256: {old_sha}", f"MD5sum: {hashlib.md5(b'o' * 9).hexdigest()}")
    ix.write_text(new)
    (ix.parent / "Packages.gz").unlink()
    rel = root / "dists/bookworm/Release"
    before = hashlib.sha256(text.encode()).hexdigest()
    rel.write_text(rel.read_text().replace(f"{before} {len(text.encode())} main/binary-amd64/Packages",
                                           f"{hashlib.sha256(new.encode()).hexdigest()} {len(new.encode())} "
                                           "main/binary-amd64/Packages"))
    (tree,) = find_trees(root)
    check = check_tree(tree, None)
    assert check.unhashed == {"dists/bookworm": 1}


def test_named_counts_md5_only_entries():
    from mmw.site.adopt import _named
    named, unhashed = _named("Package: a\nFilename: pool/a.deb\nMD5sum: x\n\nPackage: b\nFilename: pool/b.deb\n"
                             "SHA256: " + "f" * 64 + "\n", "Packages")
    assert list(named) == ["pool/b.deb"] and unhashed == {"pool/a.deb"}


def test_adopting_says_each_file_it_takes_in(site, tmp_path):
    home, _root = site
    src = make_tree(tmp_path / "old", packages={"a_1.0_amd64.deb": b"a" * 10, "b_1.0_amd64.deb": b"b" * 20})
    (tree,) = find_trees(src)
    seen = []
    adopt_tree(home, tree, check_tree(tree, None), repo=None, label="Debian", base=BASE,
               progress=lambda rel, size: seen.append((rel, size)))
    assert ("pool/main/a/a/a_1.0_amd64.deb", 10) in seen and ("pool/main/b/b/b_1.0_amd64.deb", 20) in seen


def test_the_tuis_adopt_hears_how_far_hashing_has_got(site, tmp_path):
    from mmw.tui.site.adopt_flow import AdoptChoice, _run_adopt
    home, root = site
    src = make_tree(tmp_path / "old")
    said = []
    code, _lines = _run_adopt(str(root), AdoptChoice(str(src), BASE, None, True), dry_run=True, said=said.append)
    assert code == 0 and said and "files done" in said[0]


def test_check_says_which_key_verified(upstream, tmp_path):
    make_tree(tmp_path / "t", sign_with=(upstream, "up"))
    (tree,) = find_trees(tmp_path / "t")
    check = check_tree(tree, _ring(upstream, tmp_path))
    fpr = next(k.fingerprint for k in upstream.list() if k.name == "up")
    assert check.signature == {"dists/bookworm": "verified"} and fpr in check.signed_by["dists/bookworm"]


def test_a_by_hash_file_its_release_doesnt_list_is_not_adopted(tmp_path):
    import hashlib
    root = make_tree(tmp_path / "t")
    ix = root / "dists/bookworm/main/binary-amd64/Packages"
    listed = hashlib.sha256(ix.read_bytes()).hexdigest()
    junk = b"not anything the Release lists"
    unlisted = hashlib.sha256(junk).hexdigest()
    bh = ix.parent / "by-hash/SHA256"
    bh.mkdir(parents=True)
    (bh / listed).write_bytes(ix.read_bytes())
    (bh / unlisted).write_bytes(junk)                       # named by its own hash: still nobody's
    (tree,) = find_trees(root)
    check = check_tree(tree, None)
    rel = "dists/bookworm/main/binary-amd64/by-hash/SHA256/"
    assert rel + listed in check.present and rel + unlisted not in check.present
    assert check.strays["unlisted"] == [rel + unlisted]


def test_an_installer_file_is_checked_against_its_sha256sums(tmp_path):
    import hashlib
    images = "dists/bookworm/main/installer-amd64/current/images"
    good, bad = b"a netboot image", b"a damaged image"
    sums = (f"{hashlib.sha256(good).hexdigest()}  ./netboot.tar.gz\n"
            f"{hashlib.sha256(b'the real one').hexdigest()}  ./mini.iso\n").encode()
    root = make_tree(tmp_path / "t", extra={f"{images}/netboot.tar.gz": good, f"{images}/mini.iso": bad})
    # The Release vouches for SHA256SUMS: add it to the Release's list.
    (root / images / "SHA256SUMS").write_bytes(sums)
    release = root / "dists/bookworm/Release"
    release.write_text(release.read_text() + f" {hashlib.sha256(sums).hexdigest()} {len(sums)} "
                       "main/installer-amd64/current/images/SHA256SUMS\n")
    (tree,) = find_trees(root)
    check = check_tree(tree, None)
    assert f"{images}/netboot.tar.gz" in check.present and f"{images}/mini.iso" in check.strays["damaged"]


def test_a_check_keeps_promises_only_for_what_is_missing(tmp_path):
    root = make_tree(tmp_path / "t", packages={"hello_1.0_amd64.deb": b"h" * 40, "gone_1.0_amd64.deb": b"g" * 9})
    (root / "pool/main/g/gone/gone_1.0_amd64.deb").unlink()
    (tree,) = find_trees(root)
    check = check_tree(tree, None)
    assert set(check.expected) == set(check.missing) == {"pool/main/g/gone/gone_1.0_amd64.deb"}


def test_md5_only_files_are_said_once_not_also_as_strays(tmp_path):
    import hashlib
    root = make_tree(tmp_path / "t", packages={"hello_1.0_amd64.deb": b"h" * 40, "old_1.0_amd64.deb": b"o" * 9})
    ix = root / "dists/bookworm/main/binary-amd64/Packages"
    text = ix.read_text()
    new = text.replace(f"SHA256: {hashlib.sha256(b'o' * 9).hexdigest()}", "MD5sum: x")
    ix.write_text(new)
    (ix.parent / "Packages.gz").unlink()
    rel = root / "dists/bookworm/Release"
    rel.write_text(rel.read_text().replace(f"{hashlib.sha256(text.encode()).hexdigest()} {len(text.encode())} ",
                                           f"{hashlib.sha256(new.encode()).hexdigest()} {len(new.encode())} ", 1))
    (tree,) = find_trees(root)
    check = check_tree(tree, None)
    assert check.unhashed == {"dists/bookworm": 1} and "pool/main/o/old/old_1.0_amd64.deb" not in \
        check.strays.get("pool", [])


def test_sha256sums_with_binary_markers_and_spaces(tmp_path):
    import hashlib
    images = "dists/bookworm/main/installer-amd64/current/images"
    a, b = b"image a", b"image b"
    sums = (f"{hashlib.sha256(b'other').hexdigest()} *netboot.tar.gz\n"
            f"{hashlib.sha256(b).hexdigest()}  ./my image.iso\n").encode()
    root = make_tree(tmp_path / "t", extra={f"{images}/netboot.tar.gz": a, f"{images}/my image.iso": b})
    (root / images / "SHA256SUMS").write_bytes(sums)
    release = root / "dists/bookworm/Release"
    release.write_text(release.read_text() + f" {hashlib.sha256(sums).hexdigest()} {len(sums)} "
                       "main/installer-amd64/current/images/SHA256SUMS\n")
    (tree,) = find_trees(root)
    check = check_tree(tree, None)
    assert f"{images}/netboot.tar.gz" in check.strays["damaged"] and f"{images}/my image.iso" in check.present


def test_two_repositories_sharing_a_base_share_one_dated_tree(site, tmp_path):
    """An adopted tree beside another repository at the same base: one day's
    @dir holds both suites, not whichever was built last."""
    home, root = site
    first = make_tree(tmp_path / "one", suite="bookworm", day=1)
    second = make_tree(tmp_path / "two", suite="trixie", day=1, packages={"other_1.0_amd64.deb": b"o" * 30})
    (t1,) = find_trees(first)
    (t2,) = find_trees(second)
    adopt_tree(home, t1, check_tree(t1, None), repo=None, label="Debian", base=BASE)
    adopt_tree(home, t2, check_tree(t2, None), repo="https://elsewhere.example/debian", label="Other", base=BASE)
    with SiteDB(home) as db:
        assert len(db.repo_states()) == 2
    day = root / BASE / "@2026-11-01"
    assert (day / "dists/bookworm/Release").is_file() and (day / "dists/trixie/Release").is_file()


def test_status_says_dated_trees_past_valid_until(site, tmp_path):
    from typer.testing import CliRunner

    from mmw import cli
    home, root = site
    src = make_tree(tmp_path / "old", day=1)
    rel = src / "dists/bookworm/Release"
    rel.write_text(rel.read_text().replace("Architectures:", "Valid-Until: Sun, 08 Nov 2026 00:00:00 UTC\n"
                                                             "Architectures:", 1))
    (tree,) = find_trees(src)
    adopt_tree(home, tree, check_tree(tree, None), repo=None, label="Debian", base=BASE)
    r = CliRunner().invoke(cli.app, ["site", "status", "--root", str(root)])
    assert r.exit_code == 0 and "past Valid-Until: 1" in r.output and "check-valid-until=no" in r.output, r.output


def test_a_shared_dated_tree_is_rebuilt_when_one_repository_leaves_it(site, tmp_path):
    """Two repositories share @2026-11-01; one's point there is deleted: the
    dir is rebuilt from the other alone (its files freed), not left as it was."""
    from mmw.site import ops
    home, root = site
    first = make_tree(tmp_path / "one", suite="bookworm", day=1)
    second = make_tree(tmp_path / "two", suite="trixie", day=1, packages={"other_1.0_amd64.deb": b"o" * 30})
    (t1,) = find_trees(first)
    (t2,) = find_trees(second)
    adopt_tree(home, t1, check_tree(t1, None), repo=None, label="Debian", base=BASE)
    later = make_tree(tmp_path / "two2", suite="trixie", day=2, packages={"other_2.0_amd64.deb": b"p" * 30})
    adopt_tree(home, t2, check_tree(t2, None), repo="https://elsewhere.example/debian", label="Other", base=BASE)
    (t3,) = find_trees(later)
    adopt_tree(home, t3, check_tree(t3, None), repo="https://elsewhere.example/debian", label="Other", base=BASE)
    last = make_tree(tmp_path / "two3", suite="trixie", day=3, packages={"other_3.0_amd64.deb": b"q" * 30})
    (t4,) = find_trees(last)                       # so 1 Nov is neither served nor the one before
    adopt_tree(home, t4, check_tree(t4, None), repo="https://elsewhere.example/debian", label="Other", base=BASE)
    day = root / BASE / "@2026-11-01"
    assert (day / "dists/trixie/Release").is_file()
    ops.delete_point(home, "https://elsewhere.example/debian", "2026-11-01")
    assert (day / "dists/bookworm/Release").is_file() and not (day / "dists/trixie").exists()
