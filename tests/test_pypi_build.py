"""An sdist's static build requirements: read once, cached by sha256."""
import io
import zipfile

import pytest

from mmw.providers.pypi.build import LEGACY, BuildRequires, build_requires, read_build_requires
from mmw.providers.pypi.index import IndexCache, parse_project
from pypi_fakes import FakePyPI, file_entry, real_sdist, real_sdist_bytes


def test_reads_build_system_requires():
    data = real_sdist_bytes("srconly", "1.0", build_requires=["hatchling>=1.20"])
    assert read_build_requires("srconly-1.0.tar.gz", data) == BuildRequires(["hatchling>=1.20"], None)


def test_zip_sdists_too():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("z-1.0/pyproject.toml", '[build-system]\nrequires = ["flit-core>=3"]\n')
    assert read_build_requires("z-1.0.zip", buf.getvalue()).requires == ["flit-core>=3"]


def test_no_pyproject_is_legacy_setuptools():
    data = real_sdist_bytes("old", "1.0")
    assert read_build_requires("old-1.0.tar.gz", data) == BuildRequires(LEGACY, "no pyproject.toml")
    assert LEGACY == ["setuptools>=40.8.0"]


def test_no_build_system_table_is_legacy():
    data = real_sdist_bytes("p", "1.0", pyproject='[project]\nname = "p"\n')
    assert read_build_requires("p-1.0.tar.gz", data) == BuildRequires(LEGACY, None)


def test_unparseable_pyproject_is_legacy_with_note():
    data = real_sdist_bytes("p", "1.0", pyproject="[build-system\nrequires = ")
    got = read_build_requires("p-1.0.tar.gz", data)
    assert got.requires == LEGACY and got.note.startswith("unparseable pyproject.toml")


def test_traversal_members_are_ignored():
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in (("../pyproject.toml", '[build-system]\nrequires = ["evil"]\n'),
                           ("x-1.0/sub/pyproject.toml", '[build-system]\nrequires = ["nested"]\n')):
            info = tarfile.TarInfo(name)
            info.size = len(text)
            tar.addfile(info, io.BytesIO(text.encode()))
    assert read_build_requires("x-1.0.tar.gz", buf.getvalue()) == BuildRequires(LEGACY, "no pyproject.toml")


def test_cached_by_sha(tmp_path):
    fake = FakePyPI()
    entry = real_sdist("srconly", "1.0", build_requires=["flit-core"])
    fake.add("srconly", [entry])
    cache = IndexCache(tmp_path)
    (f,), _ = parse_project("srconly", {"files": [entry]})
    assert build_requires(f, cache, fake.client).requires == ["flit-core"]
    fake.requests.clear()
    assert build_requires(f, cache, fake.client).requires == ["flit-core"]
    assert not fake.requests
    bad = FakePyPI()
    wrong = {**file_entry("other-1.0.tar.gz"), "_body": b"tampered"}
    bad.add("other", [wrong])
    (g,), _ = parse_project("other", {"files": [wrong]})
    with pytest.raises(ValueError, match="sha256"):
        build_requires(g, cache, bad.client)
    assert cache.build_requires(g.sha256) is None
