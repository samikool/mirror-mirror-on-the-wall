"""What an sdist needs to be built offline: its static `[build-system] requires`.

pip falls back to setuptools for an sdist without a pyproject.toml (or without
a `[build-system]` table), so those mean `setuptools>=40.8.0`. Only the
static list is read — requirements a backend adds at build time
(`get_requires_for_build_wheel`) are not resolved. Only the top-level
pyproject.toml of the archive is read, and only up to a size bound.
"""
import hashlib
import io
import tarfile
import tomllib
import zipfile
from dataclasses import dataclass

from mmw.providers.pypi.index import DistFile, IndexCache

LEGACY = ["setuptools>=40.8.0"]
_MAX_PYPROJECT = 1 << 20
# How much of an archive is walked looking for it: a hostile sdist must not
# make the mirror host decompress gigabytes for one lookup.
_MAX_SCAN = 256 << 20


class _GaveUp(Exception):
    pass


@dataclass
class BuildRequires:
    requires: list[str]
    note: str | None = None


def _top_level_pyproject(name: str) -> bool:
    parts = name.removeprefix("./").split("/")
    return len(parts) == 2 and parts[1] == "pyproject.toml" and parts[0] not in ("", ".", "..")


def _pyproject_text(filename: str, data: bytes) -> bytes | None:
    if filename.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                if _top_level_pyproject(info.filename) and info.file_size <= _MAX_PYPROJECT:
                    return z.read(info)
        return None
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        scanned = 0
        for member in tar:
            if member.isfile() and _top_level_pyproject(member.name) and member.size <= _MAX_PYPROJECT:
                return tar.extractfile(member).read()
            scanned += member.size
            if scanned > _MAX_SCAN:
                raise _GaveUp
    return None


def read_build_requires(filename: str, data: bytes) -> BuildRequires:
    try:
        text = _pyproject_text(filename, data)
    except _GaveUp:
        return BuildRequires(list(LEGACY), f"no top-level pyproject.toml in the first {_MAX_SCAN >> 20} MB")
    if text is None:
        return BuildRequires(list(LEGACY), "no pyproject.toml")
    try:
        system = tomllib.loads(text.decode("utf-8")).get("build-system")
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        return BuildRequires(list(LEGACY), f"unparseable pyproject.toml: {e}")
    if not isinstance(system, dict) or "requires" not in system:
        return BuildRequires(list(LEGACY))
    requires = system["requires"]
    if not isinstance(requires, list) or not all(isinstance(r, str) for r in requires):
        return BuildRequires(list(LEGACY), "build-system.requires is not a list of strings")
    return BuildRequires(list(requires))


def build_requires(f: DistFile, cache: IndexCache, client_factory) -> BuildRequires:
    """`f`'s build requirements, downloaded and read once per sha256."""
    known = cache.build_requires(f.sha256)
    if known is not None:
        return known
    with client_factory() as client:
        resp = client.get(f.url)
        resp.raise_for_status()
    if hashlib.sha256(resp.content).hexdigest() != f.sha256:
        raise ValueError(f"{f.filename}: download does not match its sha256")
    found = read_build_requires(f.filename, resp.content)
    cache.put_build_requires(f.sha256, found)
    return found
