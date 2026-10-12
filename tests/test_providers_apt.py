import hashlib
import lzma

import pytest

from mmw.core import config
from mmw.providers.apt import (
    AptFlatProvider,
    AptProvider,
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


# ── fetch_all: one snapshot, one fetch ───────────────────────────────────────


_PKG_SHA = "cccc3333" * 8


def _index_bytes() -> bytes:
    text = (
        "Package: hello\nVersion: 1.0\nArchitecture: amd64\nSize: 10\n"
        "Filename: pool/main/h/hello/hello_1.0_amd64.deb\n"
        f"SHA256: {_PKG_SHA}\n\n"
    )
    return lzma.compress(text.encode())


class _Resp:
    def __init__(self, status: int, content: bytes = b""):
        self.status_code = status
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


class _Repo:
    """An apt repository whose canonical Packages.xz has ALREADY moved on.

    This is the CDN-republish race in its sharpest form: the Release we read
    names index A, and the canonical URL is already serving index B. A provider
    that enumerates packages from the canonical URL builds a package list for a
    snapshot its own published index does not describe — so the index it
    publishes (verified against the Release) lists packages that were never
    enumerated, never downloaded, and therefore 404 for every client.
    """

    def __init__(self):
        self.named = _index_bytes()                 # what the Release points at
        self.moved_on = lzma.compress(
            b"Package: newer\nVersion: 2.0\nArchitecture: amd64\nSize: 10\n"
            b"Filename: pool/main/n/newer/newer_2.0_amd64.deb\n"
            b"SHA256: " + (b"dddd4444" * 8) + b"\n\n"
        )
        self.sha = hashlib.sha256(self.named).hexdigest()
        self.calls: list[str] = []

    def release_text(self) -> str:
        return (
            "Origin: Test\nSuite: noble\nComponents: main\n"
            "Architectures: amd64\nSHA256:\n"
            f" {self.sha} {len(self.named)} main/binary-amd64/Packages.xz\n"
        )

    def get(self, url, **kw):
        self.calls.append(url)
        if url.endswith("/dists/noble/InRelease"):
            return _Resp(200, self.release_text().encode())
        if url.endswith("/dists/noble/Release") or url.endswith("/dists/noble/Release.gpg"):
            return _Resp(404)
        if f"/by-hash/SHA256/{self.sha}" in url:
            return _Resp(200, self.named)
        if url.endswith("main/binary-amd64/Packages.xz"):
            return _Resp(200, self.moved_on)        # upstream has republished
        return _Resp(404)


def _mirror(**kw):
    return config.MirrorConfig(**{
        "name": "m", "provider": "apt", "label": "t", "arch": "amd64",
        "suite": "noble", "component": "main",
        "source_url": "https://example.test/ubuntu", **kw,
    })


def test_fetch_all_enumerates_the_index_the_release_names():
    """Packages and metadata must come from ONE snapshot."""
    repo = _Repo()
    packages, metadata = AptProvider().fetch_all(_mirror(), repo)

    assert [p.name for p in packages] == ["hello"], (
        "enumerated the canonical URL's newer index instead of the one the "
        "Release file actually names"
    )
    assert packages[0].checksum == _PKG_SHA


def test_fetch_all_fetches_the_index_once_and_reuses_it():
    """The largest file in a mirror's metadata was downloaded twice per sync:
    once to enumerate packages, once to pool it."""
    repo = _Repo()
    _, metadata = AptProvider().fetch_all(_mirror(), repo)

    index_fetches = [u for u in repo.calls if "Packages" in u or "by-hash" in u]
    assert len(index_fetches) == 1, f"index fetched {len(index_fetches)}x: {index_fetches}"

    pkg_meta = [m for m in metadata if m.repo_path.endswith("Packages.xz")]
    assert len(pkg_meta) == 1
    assert pkg_meta[0].content is not None, (
        "the bytes were fetched and thrown away — the metadata phase will "
        "fetch them all over again"
    )
    assert hashlib.sha256(pkg_meta[0].content).hexdigest() == pkg_meta[0].expected_sha256


def test_fetch_all_still_reports_an_unreadable_release():
    class _Dead:
        def get(self, url, **kw):
            return _Resp(503)

    with pytest.raises(RuntimeError, match="no usable Release file"):
        AptProvider().fetch_all(_mirror(), _Dead())


# ── flat repositories: same contract ─────────────────────────────────────────


_FLAT_TEXT = (
    b"Package: cuda\nVersion: 12\nArchitecture: amd64\nSize: 10\n"
    b"Filename: ./cuda_12_amd64.deb\nSHA256: " + (b"eeee5555" * 8) + b"\n\n"
)


class _FlatRepo:
    """A flat repo that answers HEAD, like most do."""

    def __init__(self, head_ok=True):
        self.body = lzma.compress(_FLAT_TEXT)
        self.head_ok = head_ok
        self.gets: list[str] = []

    def head(self, url, **kw):
        if not self.head_ok:
            return _Resp(405)
        return _Resp(200) if url.endswith("Packages.xz") else _Resp(404)

    def get(self, url, **kw):
        self.gets.append(url)
        return _Resp(200, self.body) if url.endswith("Packages.xz") else _Resp(404)


def _flat_mirror():
    return config.MirrorConfig(
        name="f", provider="apt-flat", label="t", arch="amd64",
        source_url="https://vendor.test/deb/amd64",
    )


def test_flat_fetch_all_reuses_the_index_it_fetched():
    """The index is GET once by the provider and then handed on, rather than
    fetched again by the metadata phase. The GET count here guards the
    provider's own half; `content is not None` is what stops the second fetch
    happening later in `download_mirror`."""
    repo = _FlatRepo()
    packages, metadata = AptFlatProvider().fetch_all(_flat_mirror(), repo)

    assert [p.name for p in packages] == ["cuda"]
    index_gets = [u for u in repo.gets if "Packages" in u]
    assert len(index_gets) == 1, f"index fetched {len(index_gets)}x: {index_gets}"
    idx = [m for m in metadata if m.repo_path == "Packages.xz"]
    assert idx and idx[0].content is not None


def test_flat_fetch_all_still_works_when_head_is_refused():
    """Some CDNs answer 405 to HEAD. `fetch_metadata` HEADs while
    `fetch_packages` GETs, so keying enumeration off the HEAD result alone
    would turn a repository that works today into a hard failure."""
    repo = _FlatRepo(head_ok=False)
    packages, _ = AptFlatProvider().fetch_all(_flat_mirror(), repo)
    assert [p.name for p in packages] == ["cuda"]


class _SignedFlatRepo(_FlatRepo):
    """A flat repository with a Release naming its index by hash — NVIDIA's
    libnvidia-container is one."""

    def __init__(self, release_hash=None):
        super().__init__()
        digest = release_hash or hashlib.sha256(self.body).hexdigest()
        self.release = (f"Origin: vendor\nSHA256:\n {digest} {len(self.body)} Packages.xz\n"
                        ).encode()

    def get(self, url, **kw):
        if url.endswith("/Release"):
            self.gets.append(url)
            return _Resp(200, self.release)
        return super().get(url, **kw)


def test_flat_release_and_index_come_from_one_snapshot():
    """The Release used to be only HEADed here and fetched hours later by the
    metadata phase, while `Packages.xz` was read up front — so a republish in
    between published a Release whose hash for `Packages.xz` matched nothing
    in the tree, and every client's `apt update` failed on it. Both are read
    now, and the index is checked against the Release that names it."""
    repo = _SignedFlatRepo()
    _packages, metadata = AptFlatProvider().fetch_all(_flat_mirror(), repo)
    by_path = {m.repo_path: m for m in metadata}
    assert by_path["Release"].content == repo.release and by_path["Release"].is_release
    assert by_path["Packages.xz"].expected_sha256 == hashlib.sha256(repo.body).hexdigest()


def test_flat_index_that_disagrees_with_its_release_fails_the_mirror():
    repo = _SignedFlatRepo(release_hash="ab" * 32)
    with pytest.raises(ValueError, match="does not match"):
        AptFlatProvider().fetch_all(_flat_mirror(), repo)


def test_flat_filename_loses_only_its_dot_slash_prefix():
    from mmw.providers.apt import _parse_packages_flat
    text = ("Package: a\nFilename: ./.cache/a.deb\nSHA256: " + "a" * 64 + "\n\n"
            "Package: b\nFilename: ../b.deb\nSHA256: " + "b" * 64 + "\n\n")
    assert [p.repo_path for p in _parse_packages_flat(text, "http://x")] == [
        ".cache/a.deb", "../b.deb"]          # the traversal is left for the safety check


def test_dist_upgrader_files_are_mirrored_only_for_dist_upgrade_sources():
    """`dist-upgrader-all/` (the release-upgrade tool tarballs) was walked for
    every `main` component, upgrade or not — the same files the source's
    `dist_upgrade` flag already gates `meta-release` on."""
    release = b"Suite: noble\nSHA256:\n"
    listing = b'<a href="noble.tar.gz">noble.tar.gz</a>'

    class _Repo:
        def get(self, url, **kw):
            if url.endswith("/InRelease"):
                return _Resp(200, release)
            if url.endswith("dist-upgrader-all/current/"):
                return _Resp(200, listing)
            return _Resp(404)

    def upgrader(**kw):
        files = AptProvider().fetch_metadata(_mirror(**kw), _Repo())
        return [f.repo_path for f in files if "dist-upgrader" in f.repo_path]

    assert upgrader() == []
    assert upgrader(dist_upgrade=True) == [
        "dists/noble/main/dist-upgrader-all/current/noble.tar.gz"]
