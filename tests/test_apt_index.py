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
