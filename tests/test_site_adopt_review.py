"""Adoption edges the final review found: Release and InRelease that
disagree, indexes absent, one file two hashes, suites matched by more than
their codename, hardlinks that can't be made, files that change midway."""
import os

import pytest

from mmw.site.adopt import adopt_tree, check_tree, find_trees
from mmw.site.db import SiteDB

from apt_tree import make_tree
from test_site_adopt import BASE, _adopt, _ring, site, upstream  # noqa: F401


def _check(path, keyring=None):
    (tree,) = find_trees(path)
    return tree, check_tree(tree, keyring)


def test_release_and_inrelease_must_agree(upstream, tmp_path):  # noqa: F811
    root = make_tree(tmp_path / "t", sign_with=(upstream, "up"))
    rel = root / "dists/bookworm/Release"
    rel.write_bytes(rel.read_bytes() + b"Edited: yes\n")
    _tree, check = _check(root, _ring(upstream, tmp_path))
    assert "Release and InRelease differ" in check.refused["dists/bookworm"]


def test_an_index_with_no_variant_here_is_missing(tmp_path):
    root = make_tree(tmp_path / "t")
    for name in ("Packages", "Packages.gz"):
        (root / "dists/bookworm/main/binary-amd64" / name).unlink()
    _tree, check = _check(root)
    assert "dists/bookworm/main/binary-amd64/Packages" in check.missing


def test_one_file_two_hashes_refuses_the_suite_it_differs_for(tmp_path):
    root = make_tree(tmp_path / "t", suite="bookworm", packages={"x_1_amd64.deb": b"first"})
    make_tree(tmp_path / "t", suite="trixie", packages={"x_1_amd64.deb": b"second"})   # same path, new bytes
    _tree, check = _check(root)
    assert set(check.refused) == {"dists/bookworm"}


def test_unreadable_files_are_missing_not_a_crash(tmp_path):
    root = make_tree(tmp_path / "t", packages={"a_1_amd64.deb": b"a", "b_1_amd64.deb": b"b"})
    p = root / "pool/main/b/b/b_1_amd64.deb"
    p.chmod(0)
    try:
        if os.access(p, os.R_OK):
            pytest.skip("running as root: nothing is unreadable")
        _tree, check = _check(root)
        assert "pool/main/b/b/b_1_amd64.deb" in check.missing
    finally:
        p.chmod(0o644)


def test_a_blank_filename_refuses_the_suite(tmp_path):
    root = make_tree(tmp_path / "t")
    ix = root / "dists/bookworm/main/binary-amd64/Packages"
    text = ix.read_text() + "\nPackage: odd\nFilename: \nSHA256: " + "0" * 64 + "\n"
    import gzip
    import hashlib
    ix.write_text(text)
    (ix.parent / "Packages.gz").write_bytes(gzip.compress(text.encode(), mtime=0))
    release = root / "dists/bookworm/Release"
    lines = []
    for ln in release.read_text().splitlines():
        parts = ln.split()
        if len(parts) == 3 and parts[2].startswith("main/binary-amd64/Packages"):
            data = (root / "dists/bookworm" / parts[2]).read_bytes()
            ln = f" {hashlib.sha256(data).hexdigest()} {len(data)} {parts[2]}"
        lines.append(ln)
    release.write_text("\n".join(lines) + "\n")
    _tree, check = _check(root)
    assert "outside the tree" in check.refused["dists/bookworm"]


def test_a_by_hash_file_that_is_not_its_name_is_not_adopted(tmp_path):
    root = make_tree(tmp_path / "t", extra={"dists/bookworm/main/binary-amd64/by-hash/SHA256/" + "0" * 64: b"x"})
    _tree, check = _check(root)
    assert check.strays.get("damaged") == ["dists/bookworm/main/binary-amd64/by-hash/SHA256/" + "0" * 64]


def test_another_origin_with_the_same_codename_is_not_matched(site, tmp_path):  # noqa: F811
    from mmw.site.adopt import matching_repo as _matching_repo
    home, _root = site
    _adopt(home, make_tree(tmp_path / "debian", origin="Debian"))
    (docker,) = find_trees(make_tree(tmp_path / "docker", origin="Docker"))
    (debian,) = find_trees(tmp_path / "debian")
    with SiteDB(home) as db:
        assert _matching_repo(db, docker) is None
        assert _matching_repo(db, debian)[1] == BASE


def test_the_match_names_the_base_its_track_is_under(site):  # noqa: F811
    from mmw.site.adopt import matching_repo as _matching_repo
    from mmw.site.adopt import FoundTrack, FoundTree
    home, _root = site
    with SiteDB(home) as db:
        db.ensure_repo("r", "R", ["one.example/a", "two.example/b"])
        db.ensure_track("r", "two.example/b/dists/bookworm", {"Origin": "Debian", "Label": "Debian",
                                                               "Codename": "bookworm"})
    tree = FoundTree(tmp := __import__("pathlib").Path("/x"), False,
                     [FoundTrack("dists/bookworm", {"Origin": "Debian", "Label": "Debian", "Codename": "bookworm"},
                                 "20261101T000000Z", "release:x")])
    with SiteDB(home) as db:
        assert _matching_repo(db, tree) == ("r", "two.example/b")
    assert tmp


def test_a_file_that_cannot_be_hardlinked_is_copied(site, tmp_path, monkeypatch):  # noqa: F811
    home, root = site
    import mmw.site.adopt as A

    def refuse(*a, **k):
        raise PermissionError("protected_hardlinks")
    monkeypatch.setattr(A.os, "link", refuse)
    r = _adopt(home, make_tree(tmp_path / "t"))
    assert r.copied and not r.linked
    assert (root / BASE / "dists/bookworm/Release").exists()


def test_a_file_only_its_owner_can_read_is_copied(site, tmp_path):  # noqa: F811
    home, root = site
    src = make_tree(tmp_path / "t")
    deb = src / "pool/main/h/hello/hello_1.0_amd64.deb"
    deb.chmod(0o600)
    _adopt(home, src)
    served = root / BASE / "pool/main/h/hello/hello_1.0_amd64.deb"
    assert served.stat().st_ino != deb.stat().st_ino and served.stat().st_mode & 0o004


def test_a_file_changed_after_checking_is_refused(site, tmp_path):  # noqa: F811
    home, root = site
    src = make_tree(tmp_path / "t")
    (tree,) = find_trees(src)
    check = check_tree(tree, None)
    deb = src / "pool/main/h/hello/hello_1.0_amd64.deb"
    tmp = deb.with_suffix(".new")
    tmp.write_bytes(b"replaced")
    tmp.rename(deb)                                   # a mirror tool renaming a new file in
    with pytest.raises(ValueError, match="changed"):
        adopt_tree(home, tree, check, repo=None, label="Debian", base=BASE)


def test_a_bundles_point_is_not_marked_unverified(site, tmp_path):  # noqa: F811
    home, _root = site
    src = make_tree(tmp_path / "t")
    first = _adopt(home, src)
    with SiteDB(home) as db:
        for k in list(db.metas("unverified:")):
            db._conn.execute("DELETE FROM meta WHERE key = ?", (k,))
        db.commit()
    again = _adopt(home, src)                          # the same point: not new
    assert first.unverified and not again.unverified


def test_adopting_again_keeps_a_repositorys_mirror_entry(site, tmp_path):  # noqa: F811
    import json
    home, _root = site
    r = _adopt(home, make_tree(tmp_path / "t1", day=1))
    with SiteDB(home) as db:
        known = json.loads(db.get_meta("foreign_mirrors"))
        known[r.repo][0]["label"] = "Kept"
        db.set_meta("foreign_mirrors", json.dumps(known))
    _adopt(home, make_tree(tmp_path / "t2", day=2))
    with SiteDB(home) as db:
        assert json.loads(db.get_meta("foreign_mirrors"))[r.repo][0]["label"] == "Kept"
