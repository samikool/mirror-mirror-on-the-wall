"""apt index building: a deb's control, Packages, Contents and Release."""
import pytest

from debs import make_deb
from mmw.core.apt_index import (
    Entry, compressed, contents_text, data_paths, packages_text, read_control, release_text, rfc2822,
)


def test_reads_control_in_every_compression(tmp_path):
    for comp in ("gz", "xz", "zst"):
        deb = make_deb(tmp_path / comp, "tool", "1.2-1", compression=comp,
                       extra={"Depends": "libc6 (>= 2.34)"})
        c = read_control(deb)
        assert (c["Package"], c["Version"], c["Architecture"]) == ("tool", "1.2-1", "amd64")
        assert c["Depends"] == "libc6 (>= 2.34)"
        assert c["Description"] == "tool for tests\n test package"


def test_not_a_deb_names_the_file(tmp_path):
    bad = tmp_path / "x.deb"
    bad.write_bytes(b"nope")
    with pytest.raises(ValueError, match="x.deb"):
        read_control(bad)


def test_packages_text_is_sorted_and_complete():
    a = Entry({"Package": "b", "Version": "1", "Architecture": "all"}, "./b_1_all.deb", 10, "s1", "m1")
    b = Entry({"Package": "a", "Version": "2", "Architecture": "amd64", "Size": "99"},
              "./a_2_amd64.deb", 20, "s2", "m2")
    text = packages_text([a, b]).decode()
    assert text.index("Package: a") < text.index("Package: b")
    assert "Filename: ./a_2_amd64.deb\nSize: 20\nMD5sum: m2\nSHA256: s2\n" in text
    assert "Size: 99" not in text
    assert "\n\nPackage: b" in text
    assert packages_text([]) == b""


def test_compression_is_deterministic():
    assert compressed("Packages", b"x") == compressed("Packages", b"x")
    assert set(compressed("Packages", b"x")) == {"Packages", "Packages.gz", "Packages.xz"}


def test_contents_lists_each_path_with_its_packages(tmp_path):
    deb = make_deb(tmp_path, "tool", "1.0")
    assert data_paths(deb) == ["usr/share/doc/tool/README"]
    assert contents_text([("usr/bin/x", "utils/a"), ("usr/bin/x", "utils/b"), ("etc/y", "admin/c")]) == \
        b"etc/y  admin/c\nusr/bin/x  utils/a,utils/b\n"


def test_release_lists_every_file_with_both_hashes():
    rel = release_text({"Origin": "o", "Date": rfc2822(0)}, {"b": b"22", "a": b"1"}).decode()
    assert rel.startswith("Origin: o\nDate: Thu, 01 Jan 1970 00:00:00 UTC\n")
    sha = rel.split("SHA256:\n", 1)[1].splitlines()
    assert sha[0].endswith(" 1 a") and sha[1].endswith(" 2 b")
    assert "MD5Sum:\n" in rel


# ── plan 2: Release rewriting, versions ──────────────────────────────────────

from mmw.core.apt_index import parse_packages, release_body, rewrite_release, version_cmp  # noqa: E402

_REL = (b"Origin: Ubuntu\nSuite: jammy\nDate: Thu, 21 Apr 2022 17:16:08 UTC\n"
        b"Valid-Until: Thu, 28 Apr 2022 17:16:08 UTC\nAcquire-By-Hash: yes\n"
        b"MD5Sum:\n 0123 10 main/binary-amd64/Packages\n 4567 20 main/binary-amd64/Packages.gz\n"
        b" 89ab 30 universe/binary-amd64/Packages.gz\n"
        b"SHA256:\n aaaa 10 main/binary-amd64/Packages\n bbbb 20 main/binary-amd64/Packages.gz\n"
        b" cccc 30 universe/binary-amd64/Packages.gz\n")


def test_release_body_from_inrelease():
    signed = (b"-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA512\n\nOrigin: x\n- -dashed\n"
              b"-----BEGIN PGP SIGNATURE-----\n\nabc\n-----END PGP SIGNATURE-----\n")
    assert release_body(signed) == b"Origin: x\n-dashed\n"
    assert release_body(b"Origin: x\n") == b"Origin: x\n"


def test_rewrite_release_updates_every_hash_section_and_appends_new():
    import hashlib
    new = {"main/binary-amd64/Packages.gz": b"new gz", "main/binary-amd64/Packages.xz": b"xz!"}
    text = rewrite_release(_REL, new).decode()
    md5 = hashlib.md5(b"new gz").hexdigest()
    sha = hashlib.sha256(b"new gz").hexdigest()
    assert f" {md5} {6:>16} main/binary-amd64/Packages.gz" in text
    assert f" {sha} {6:>16} main/binary-amd64/Packages.gz" in text
    assert text.count("main/binary-amd64/Packages.xz") == 2           # appended to both sections
    assert " cccc 30 universe/binary-amd64/Packages.gz" in text         # untouched as upstream wrote it
    assert "Valid-Until: Thu, 28 Apr 2022" in text and "Date: Thu, 21 Apr 2022" in text


def test_rewrite_release_drops_valid_until():
    assert b"Valid-Until" not in rewrite_release(_REL, {}, drop_valid_until=True)


def test_version_cmp():
    order = ["1.0~rc1", "1.0", "1.0-1", "1.0-1ubuntu1", "1.1", "2.9", "2.10", "1:0.1"]
    for a, b in zip(order, order[1:]):
        assert version_cmp(a, b) < 0 and version_cmp(b, a) > 0, (a, b)
    assert version_cmp("1.0-1", "1.0-1") == 0
    assert version_cmp("0:1.0", "1.0") == 0


def test_parse_packages():
    stanzas = parse_packages("Package: a\nVersion: 1\nDescription: x\n more\n\nPackage: b\nVersion: 2\n")
    assert [s["Package"] for s in stanzas] == ["a", "b"]
    assert stanzas[0]["Description"] == "x\n more"
