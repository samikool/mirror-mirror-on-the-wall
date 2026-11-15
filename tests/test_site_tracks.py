"""A repository's files split into one point per upstream Release (a track),
plus the files no Release claims."""
from site_apt_rows import Repo, clearsign

from mmw.site.tracks import release_date, release_ident, split_tracks

D1 = "Sat, 09 Nov 2026 10:02:03 UTC"
FALLBACK = "20261114T000000Z"


def _debian() -> Repo:
    r = Repo()
    r.deb("pool/main/a/a_1.deb")
    r.deb("pool/main/b/b_2.deb")
    r.deb("pool/main/c/c_1.dsc")
    r.deb("pool/main/c/c_1.tar.xz")
    r.suite("bookworm", D1, ["pool/main/a/a_1.deb", "pool/main/b/b_2.deb"],
            sources={"pool/main/c": ["c_1.dsc", "c_1.tar.xz"]})
    r.suite("bookworm-updates", D1, ["pool/main/b/b_2.deb"], compress="", inrelease=False)
    r.put("README", b"hello")
    return r


def _by_track(drafts):
    return {d.track: d for d in drafts}


def test_split_by_release_dir():
    r = _debian()
    got = _by_track(split_tracks(r.rows, [r.base], r.read, FALLBACK))
    assert set(got) == {"ex.org/debian/dists/bookworm", "ex.org/debian/dists/bookworm-updates", ""}
    bw, up, rest = got["ex.org/debian/dists/bookworm"], got["ex.org/debian/dists/bookworm-updates"], got[""]
    assert "ex.org/debian/pool/main/b/b_2.deb" in bw.files and "ex.org/debian/pool/main/b/b_2.deb" in up.files
    assert "ex.org/debian/pool/main/a/a_1.deb" in bw.files and "ex.org/debian/pool/main/a/a_1.deb" not in up.files
    assert {"ex.org/debian/pool/main/c/c_1.dsc", "ex.org/debian/pool/main/c/c_1.tar.xz"} <= set(bw.files)
    assert "ex.org/debian/dists/bookworm/InRelease" in bw.files
    assert set(rest.files) == {"ex.org/debian/README"}
    assert bw.fields["Suite"] == "bookworm" and bw.ident.startswith("release:")
    assert bw.date == "20261109T100203Z"
    # Every row lands somewhere.
    assert set().union(*(d.files for d in got.values())) == set(r.rows)


def test_release_ident_same_for_release_and_inrelease():
    rel = b"Origin: Debian\nSuite: bookworm\nDate: x\nSHA256:\n abc 1 main/Packages\n"
    signed = clearsign(rel)
    assert release_ident(rel, None) == release_ident(None, signed) == release_ident(rel, signed)
    assert release_ident(rel, None) != release_ident(rel + b"X: y\n", None)


def test_release_date():
    assert release_date(f"Origin: x\nDate: {D1}\n", FALLBACK) == ("20261109T100203Z", None)


def test_release_date_falls_back():
    for text in ("Origin: x\n", "Date: garbage\n"):
        stamp, warning = release_date(text, FALLBACK)
        assert stamp == FALLBACK and warning


def test_missing_date_warns_on_the_draft():
    r = Repo()
    r.suite("sid", None, [])
    (d,) = split_tracks(r.rows, [r.base], r.read, FALLBACK)
    assert d.date == FALLBACK and any("ex.org/debian/dists/sid" in w for w in d.warnings)


def test_flat_repo_track_is_the_base():
    r = Repo("ex.org/flat")
    r.deb("x_1.deb")
    r.suite("flat", D1, ["x_1.deb"], flat=True, compress="gz")
    (d,) = split_tracks(r.rows, [r.base], r.read, FALLBACK)
    assert d.track == "ex.org/flat" and "ex.org/flat/x_1.deb" in d.files


def test_unreadable_index_is_reported():
    r = _debian()
    idx = "ex.org/debian/dists/bookworm/main/binary-amd64/Packages.xz"
    got = _by_track(split_tracks(r.rows, [r.base], lambda p: None if p == idx else r.read(p), FALLBACK))
    assert got["ex.org/debian/dists/bookworm"].unreadable == (idx,)
    assert "ex.org/debian/pool/main/a/a_1.deb" in got[""].files


def test_no_release_is_one_content_point():
    r = Repo("example.com/a")
    r.put("a.deb", b"a")
    r.put("Packages", b"Package: a\n")
    (d,) = split_tracks(r.rows, [r.base], r.read, FALLBACK)
    assert d.track == "" and d.ident.startswith("content:") and d.date == FALLBACK
    assert set(d.files) == set(r.rows)


def test_filename_with_dot_slash():
    r = Repo()
    r.deb("pool/x_1.deb")
    r.suite("sid", D1, ["./pool/x_1.deb"], compress="")
    (d,) = split_tracks(r.rows, [r.base], r.read, FALLBACK)
    assert "ex.org/debian/pool/x_1.deb" in d.files
