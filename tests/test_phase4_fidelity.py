"""Phase 4: APT index completeness + RPM snapshot consistency."""
import pytest

from mmw.core.config import KNOWN_ARCHES as _KNOWN_ARCHES
from mmw.providers.apt import _wanted_index_file

AMD64_OTHERS = _KNOWN_ARCHES - {"amd64"}


@pytest.mark.parametrize("path", [
    "main/binary-amd64/Packages.xz",
    "main/binary-amd64/Release",
    "main/i18n/Translation-en.xz",
    "main/i18n/Translation-de.bz2",
    "main/cnf/Commands-amd64.xz",
    "main/dep11/Components-amd64.yml.xz",
    "main/dep11/icons-64x64.tar.gz",
    "main/Contents-amd64.gz",
    "Contents-amd64.gz",
    "InRelease",
    "main/debian-installer/binary-amd64/Packages.gz",
])
def test_wanted_for_main_amd64(path):
    assert _wanted_index_file(path, "main", AMD64_OTHERS, include_sources=False)


@pytest.mark.parametrize("path", [
    "contrib/binary-amd64/Packages.xz",        # other component
    "main/binary-i386/Packages.xz",            # other arch
    "main/cnf/Commands-arm64.xz",              # other arch
    "Contents-i386.gz",                        # other arch, top level
    "main/source/Sources.xz",                  # sources excluded by default
    "main/Contents-mips64el.gz",
])
def test_unwanted_for_main_amd64(path):
    assert not _wanted_index_file(path, "main", AMD64_OTHERS, include_sources=False)


def test_sources_included_when_requested():
    assert _wanted_index_file("main/source/Sources.xz", "main", AMD64_OTHERS, include_sources=True)


def test_component_matching_is_segment_based():
    """component "free" must not match the "non-free" subtree (and vice versa)."""
    others = _KNOWN_ARCHES - {"amd64"}
    assert not _wanted_index_file("non-free/binary-amd64/Packages.xz", "free", others, False)
    assert _wanted_index_file("non-free/binary-amd64/Packages.xz", "non-free", others, False)


def test_arch_matching_is_token_based():
    """arch "mips" must not exclude its own files due to "mipsel" containing "mips"."""
    others = _KNOWN_ARCHES - {"mips"}
    assert _wanted_index_file("main/binary-mips/Packages.xz", "main", others, False)
    assert not _wanted_index_file("main/binary-mipsel/Packages.xz", "main", others, False)


# ── RPM fetch_all: one repomd fetch, consistent views ─────────────────────────

REPOMD = b"""<?xml version="1.0"?>
<repomd xmlns="http://linux.duke.edu/metadata/repo">
  <data type="primary">
    <checksum type="sha256">abc</checksum>
    <location href="repodata/primary.xml"/>
  </data>
</repomd>"""

PRIMARY = b"""<?xml version="1.0"?>
<metadata xmlns="http://linux.duke.edu/metadata/common">
  <package type="rpm">
    <name>bash</name>
    <checksum type="sha256" pkgid="YES">c1</checksum>
    <size package="10"/>
    <location href="Packages/bash.rpm"/>
  </package>
</metadata>"""


class _RepoClient:
    def __init__(self):
        self.repomd_fetches = 0

    def get(self, url):
        class R:
            status_code = 200
            def raise_for_status(self): ...
        r = R()
        if url.endswith("repomd.xml"):
            self.repomd_fetches += 1
            r.content = REPOMD
        elif url.endswith("repomd.xml.asc"):
            r.status_code = 404
            r.content = b""
        elif url.endswith("primary.xml"):
            r.content = PRIMARY
        else:
            raise AssertionError(f"unexpected fetch: {url}")
        return r




def test_loong64_and_hyphenated_arches_are_told_apart():
    others = _KNOWN_ARCHES - {"amd64"}
    assert not _wanted_index_file("main/binary-loong64/Packages.xz", "main", others, False)
    assert not _wanted_index_file("Contents-loong64.gz", "main", others, False)
    i386_others = _KNOWN_ARCHES - {"i386"}
    assert not _wanted_index_file("main/binary-hurd-i386/Packages.xz", "main", i386_others, False)
    assert _wanted_index_file("main/binary-i386/Packages.xz", "main", i386_others, False)
    hurd_others = _KNOWN_ARCHES - {"hurd-i386"}
    assert _wanted_index_file("main/binary-hurd-i386/Packages.xz", "main", hurd_others, False)
    assert _wanted_index_file("main/Contents-hurd-i386.gz", "main", hurd_others, False)
