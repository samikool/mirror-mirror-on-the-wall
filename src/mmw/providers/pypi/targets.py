"""What one target — a Python minor version on a platform and arch — installs.

A target answers two different questions about a wheel. `priority` is "can a
client at the source's OS *floor* install it, and how much does pip prefer
it": that decides which release counts as installable, so "latest" never
lands on a release only newer machines can use. `keeps` is "is it for this
target at all, on the floor or any newer OS": every such variant of a kept
release is mirrored, so newer clients find the wheel they would pick.
"""
from dataclasses import dataclass
from functools import lru_cache
from itertools import product

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.tags import Tag, compatible_tags, cpython_tags, mac_platforms
from packaging.version import Version

PYTHONS = ("3.9", "3.10", "3.11", "3.12", "3.13", "3.14")
PLATFORMS = ("linux-glibc", "linux-musl", "windows", "macos")
ARCHES = ("amd64", "arm64")

# "Any newer OS" for `keeps`: ceilings well past every release that exists.
GLIBC_MAX = (2, 60)
MUSL_MAX = (1, 9)
MACOS_MAX = (30, 0)

_LINUX_ARCH = {"amd64": "x86_64", "arm64": "aarch64"}
_MAC_ARCH = {"amd64": "x86_64", "arm64": "arm64"}
_WIN_PLATFORM = {"amd64": "win_amd64", "arm64": "win_arm64"}
# The pre-PEP 600 names and the glibc they stand for.
_MANYLINUX_ALIASES = {(2, 17): "manylinux2014", (2, 12): "manylinux2010", (2, 5): "manylinux1"}


@dataclass(frozen=True)
class Floors:
    glibc: str = "2.31"
    musl: str = "1.2"
    macos: str = "11.0"


@dataclass(frozen=True)
class Target:
    python: str
    platform: str
    arch: str

    @property
    def key(self) -> str:
        return f"{self.python}-{self.platform}-{self.arch}"

    @property
    def minor(self) -> int:
        return int(self.python.split(".")[1])


def targets(pythons, platforms, arches) -> list[Target]:
    """Every target the lists select; an empty list means all of its values."""
    return [Target(*t) for t in product(pythons or PYTHONS, platforms or PLATFORMS, arches or ARCHES)]


def _version(text: str) -> tuple[int, int]:
    major, minor = text.split(".")
    return int(major), int(minor)


def _platforms(target: Target, ceiling: tuple[int, int]) -> list[str]:
    """Platform tags a client at OS version `ceiling` accepts, most preferred first."""
    if target.platform == "windows":
        return [_WIN_PLATFORM[target.arch]]
    if target.platform == "macos":
        return list(mac_platforms(ceiling, _MAC_ARCH[target.arch]))
    arch = _LINUX_ARCH[target.arch]
    if target.platform == "linux-musl":
        return [f"musllinux_{ceiling[0]}_{minor}_{arch}" for minor in range(ceiling[1], -1, -1)]
    out = []
    for minor in range(ceiling[1], 4, -1):
        out.append(f"manylinux_2_{minor}_{arch}")
        alias = _MANYLINUX_ALIASES.get((2, minor))
        if alias:
            out.append(f"{alias}_{arch}")
    return out


def _ordered_tags(target: Target, ceiling: tuple[int, int]) -> list[Tag]:
    version = (3, target.minor)
    plats = _platforms(target, ceiling)
    return [*cpython_tags(python_version=version, abis=[f"cp3{target.minor}"], platforms=plats),
            *compatible_tags(python_version=version, interpreter=f"cp3{target.minor}", platforms=plats)]


class TagMatcher:
    def __init__(self, target: Target, floors: Floors):
        self.target = target
        floor = {"linux-glibc": floors.glibc, "linux-musl": floors.musl,
                 "macos": floors.macos}.get(target.platform)
        ceiling = {"linux-glibc": GLIBC_MAX, "linux-musl": MUSL_MAX, "macos": MACOS_MAX}.get(target.platform, (0, 0))
        at_floor = _ordered_tags(target, _version(floor) if floor else (0, 0))
        self._priority = {tag: i for i, tag in reversed(list(enumerate(at_floor)))}
        self._any = set(_ordered_tags(target, ceiling))

    def priority(self, tags: frozenset[Tag]) -> int | None:
        """pip's preference for this wheel at the floor (lower wins); None when
        a client at the floor cannot install it."""
        found = [self._priority[t] for t in tags if t in self._priority]
        return min(found) if found else None

    def keeps(self, tags: frozenset[Tag]) -> bool:
        return any(t in self._any for t in tags)


def is_universal(tags: frozenset[Tag]) -> bool:
    return bool(tags) and all(t.platform == "any" for t in tags)


def requires_python_ok(spec: str | None, target: Target) -> bool:
    """Does `Requires-Python` admit the target? Unparseable counts as yes, as
    pip ignores a specifier it cannot read."""
    return True if not spec else _requires_python_ok(spec, target.minor)


@lru_cache(maxsize=1 << 14)
def _requires_python_ok(spec: str, minor: int) -> bool:
    # A few thousand distinct specifiers across all of PyPI, asked millions of times.
    try:
        return SpecifierSet(spec).contains(Version(f"3.{minor}.0"), prereleases=True)
    except InvalidSpecifier:
        return True


def marker_env(target: Target) -> dict[str, str]:
    linux = target.platform.startswith("linux")
    if linux:
        system, sys_platform, machine = "Linux", "linux", _LINUX_ARCH[target.arch]
    elif target.platform == "windows":
        system, sys_platform, machine = "Windows", "win32", target.arch.upper()
    else:
        system, sys_platform, machine = "Darwin", "darwin", _MAC_ARCH[target.arch]
    full = f"3.{target.minor}.0"
    return {
        "python_version": target.python, "python_full_version": full,
        "implementation_name": "cpython", "implementation_version": full,
        "platform_python_implementation": "CPython",
        "sys_platform": sys_platform, "platform_system": system,
        "os_name": "nt" if target.platform == "windows" else "posix",
        "platform_machine": machine, "platform_release": "", "platform_version": "",
        "extra": "",
    }
