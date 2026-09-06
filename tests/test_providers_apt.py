from mmw.providers.apt import (
    _parse_packages,
    _parse_packages_flat,
    _parse_release_files,
    _parse_sources,
)

PACKAGES_TEXT = """\
Package: hello
Version: 2.10-3
Architecture: amd64
Size: 53080
Filename: pool/main/h/hello/hello_2.10-3_amd64.deb
SHA256: aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111
Description: example package
 a continuation line

Package: world
Version: 1.0
Architecture: amd64
Size: 1000
Filename: pool/main/w/world/world_1.0_amd64.deb
SHA256: bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222
"""


def test_parse_packages():
    pkgs = _parse_packages(PACKAGES_TEXT, "https://deb.debian.org/debian")
    assert len(pkgs) == 2
    hello = pkgs[0]
    assert hello.name == "hello"
    assert hello.checksum == "aaaa1111" * 8
    assert hello.size == 53080
    assert hello.repo_path == "pool/main/h/hello/hello_2.10-3_amd64.deb"
    assert hello.filename == "hello_2.10-3_amd64.deb"
    assert hello.url == "https://deb.debian.org/debian/pool/main/h/hello/hello_2.10-3_amd64.deb"


def test_parse_packages_skips_incomplete_blocks():
    text = "Package: nofile\nVersion: 1\n\n" + PACKAGES_TEXT
    pkgs = _parse_packages(text, "http://x")
    assert [p.name for p in pkgs] == ["hello", "world"]


def test_parse_packages_flat_strips_dot_slash():
    text = PACKAGES_TEXT.replace("Filename: pool/", "Filename: ./pool/")
    pkgs = _parse_packages_flat(text, "http://x")
    assert pkgs[0].repo_path == "pool/main/h/hello/hello_2.10-3_amd64.deb"


RELEASE_TEXT = """\
Origin: Debian
Suite: stable
Codename: bookworm
Architectures: amd64 arm64
Components: main contrib non-free
SHA256:
 cccc3333cccc3333cccc3333cccc3333cccc3333cccc3333cccc3333cccc3333  1234 main/binary-amd64/Packages.xz
 dddd4444dddd4444dddd4444dddd4444dddd4444dddd4444dddd4444dddd4444  5678 main/binary-amd64/Packages.gz
Not-A-Hash-Line: trailing
"""


def test_parse_release_files():
    files = _parse_release_files(RELEASE_TEXT)
    assert len(files) == 2
    checksum, path, size = files[0]
    assert checksum.startswith("cccc3333")
    assert path == "main/binary-amd64/Packages.xz"
    assert size == 1234


SOURCES_TEXT = """\
Package: hello
Directory: pool/main/h/hello
Checksums-Sha256:
 eeee5555eeee5555eeee5555eeee5555eeee5555eeee5555eeee5555eeee5555 2000 hello_2.10-3.dsc
 ffff6666ffff6666ffff6666ffff6666ffff6666ffff6666ffff6666ffff6666 90000 hello_2.10.orig.tar.gz
Standards-Version: 4.6.0
"""


def test_parse_sources():
    pkgs = _parse_sources(SOURCES_TEXT, "http://x")
    assert len(pkgs) == 2
    assert pkgs[0].repo_path == "pool/main/h/hello/hello_2.10-3.dsc"
    assert pkgs[0].size == 2000
    assert pkgs[1].filename == "hello_2.10.orig.tar.gz"
