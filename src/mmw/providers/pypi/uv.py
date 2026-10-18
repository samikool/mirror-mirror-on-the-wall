"""Resolve a requirements list for one target with uv's backtracking resolver.

`uv pip compile` against the index, for the target's Python version and the
platform its OS floors describe. The list resolves *together*, like
`pip download -r`. Wheels are preferred: a solution that needs a build from
source is only taken when no wheels-only one exists. uv runs with no config
file and none of the user's `UV_*` / `PIP_*` variables, so nothing outside
mmw.toml can point it at another index.
"""
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

from mmw.providers.pypi.targets import Floors, Target

INDEX_URL = "https://pypi.org/simple"

# The manylinux floors uv's --python-platform names (besides manylinux2014 = 2.17).
_UV_GLIBC = [(2, 17), (2, 28), *((2, minor) for minor in range(31, 41))]
_ARCH = {"amd64": "x86_64", "arm64": "aarch64"}


def platform_triple(target: Target, floors: Floors) -> str:
    """uv's name for the target at its floor: the highest manylinux uv knows
    that does not exceed the glibc floor (2.17 if the floor is older still)."""
    arch = _ARCH[target.arch]
    if target.platform == "linux-glibc":
        major, minor = (int(x) for x in floors.glibc.split("."))
        best = max((v for v in _UV_GLIBC if v <= (major, minor)), default=_UV_GLIBC[0])
        return f"{arch}-manylinux_{best[0]}_{best[1]}"
    if target.platform == "linux-musl":
        return f"{arch}-unknown-linux-musl"
    if target.platform == "windows":
        return f"{arch}-pc-windows-msvc"
    return f"{arch}-apple-darwin"


@dataclass
class Compiled:
    pins: list[tuple[str, str]]     # (canonical name, version)
    error: str | None               # the tail of uv's stderr when it failed
    built: bool                     # True: only a solution with source builds exists


def _env(target: Target, floors: Floors) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("UV_", "PIP_")) and k != "MACOSX_DEPLOYMENT_TARGET"}
    if target.platform == "macos":
        env["MACOSX_DEPLOYMENT_TARGET"] = floors.macos
    return env


def _pins(stdout: str) -> list[tuple[str, str]]:
    pins = []
    for line in stdout.splitlines():
        line = line.split(" #", 1)[0].strip()
        if not line or line.startswith(("#", "-")):
            continue
        try:
            req = Requirement(line)
        except InvalidRequirement:
            continue
        exact = [s.version for s in req.specifier if s.operator == "=="]
        if len(exact) == 1:
            pins.append((canonicalize_name(req.name), exact[0]))
    return pins


def _tail(text: str, lines: int = 12) -> str:
    return "\n".join(text.strip().splitlines()[-lines:]) or "uv failed without a message"


def compile_for(lines: list[str], target: Target, floors: Floors, *, index_url: str = INDEX_URL,
                cache_dir: Path, timeout: float = 1800) -> Compiled:
    from uv import find_uv_bin
    base = [find_uv_bin(), "--no-config", "pip", "compile", "-",
            "--python-version", target.python, "--python-platform", platform_triple(target, floors),
            "--no-header", "--no-annotate", "--default-index", index_url, "--cache-dir", str(cache_dir)]
    env = _env(target, floors)
    text = "\n".join(lines) + "\n"
    try:
        wheels = subprocess.run([*base, "--only-binary", ":all:"], input=text, text=True,
                                capture_output=True, env=env, timeout=timeout)
        if wheels.returncode == 0:
            return Compiled(_pins(wheels.stdout), None, False)
        any_dist = subprocess.run(base, input=text, text=True, capture_output=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        # One target's failure, never the source's: the straight mirror
        # resolved in the same pass must still ship.
        return Compiled([], f"uv did not finish within {timeout:.0f} s", False)
    except OSError as e:
        return Compiled([], f"uv could not be run: {e}", False)
    if any_dist.returncode == 0:
        return Compiled(_pins(any_dist.stdout), None, True)
    return Compiled([], _tail(any_dist.stderr), False)
