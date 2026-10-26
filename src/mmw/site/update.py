"""The far side's newer mmw: the installer file next to a bundle (it never
asks a network). One update covers every machine of the mirror — this one
and each ssh root — or none: a remote mmw older than this one can't be used,
so a half-updated mirror is the state to avoid."""
import platform
import re
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from mmw.core import disk_io
from mmw.core import update as U

_NAME = re.compile(r"^mmw-(?P<version>[^-]+(?:-[^-]+)*)-(?P<arch>x86_64|aarch64)\.tar$")


def machine_arch() -> str:
    return {"arm64": "aarch64"}.get(platform.machine(), platform.machine())


def _installers_beside(src: str) -> dict[str, dict[str, Path]]:
    """version → arch → installer, next to a bundle .tar or a stage folder."""
    if src == "-":
        return {}
    folder = Path(src).expanduser().resolve().parent
    out: dict[str, dict[str, Path]] = {}
    try:
        entries = list(disk_io.ops.iterdir(folder))
    except OSError:
        return {}
    for p in entries:
        m = _NAME.match(p.name)
        if m and disk_io.ops.is_file(p):
            out.setdefault(m["version"], {})[m["arch"]] = p
    return out


def _newest_above(versions, current: str) -> str | None:
    from packaging.version import InvalidVersion, Version
    best = None
    for v in versions:
        try:
            if Version(v) > Version(current) and (best is None or Version(v) > Version(best)):
                best = v
        except InvalidVersion:
            continue
    return best


def installer_beside(src: str, arch: str) -> tuple[Path, str] | None:
    """The newest installer for `arch` next to `src`, newer than this mmw."""
    found = {v: archs[arch] for v, archs in _installers_beside(src).items() if arch in archs}
    newest = _newest_above(found, U.current_version())
    return (found[newest], newest) if newest else None


@dataclass
class FarOffer:
    offer: U.Offer
    installers: dict[str, Path]                       # arch → installer of `offer.newest`
    remotes: list = field(default_factory=list)       # the mirror's ssh roots


def _arch_of(root) -> str:
    return root.arch() if hasattr(root, "arch") else machine_arch()


def far_offer(src: str, mset) -> FarOffer | None:
    """A newer mmw next to the bundle at `src`, for this machine's arch."""
    mine = installer_beside(src, machine_arch())
    if mine is None:
        return None
    _path, version = mine
    installers = _installers_beside(src).get(version, {})
    remotes = [r for r in (mset.roots if mset is not None else []) if getattr(r, "host", None)]
    return FarOffer(U.offer(version), installers, remotes)


def preflight(far: FarOffer) -> str | None:
    """Why the update can't cover every machine, or None — this machine
    first, so no remote is ever updated for a machine that can't follow."""
    if U.installer_prefix() is None:
        return ("this mmw wasn't installed by mmw's installer, so it can't update from the installer next to "
                "the bundle — update it the way it was installed; nothing was updated")
    for root in far.remotes:
        try:
            arch = _arch_of(root)
        except Exception as e:  # noqa: BLE001 — unreachable, or it won't say: never guessed
            return f"root {root.info.name} ({root.info.location}): can't tell its architecture ({e}) — nothing was updated"
        if arch not in far.installers:
            return (f"root {root.info.name} ({root.info.location}) is {arch}, and there's no "
                    f"mmw-{far.offer.newest}-{arch}.tar next to the bundle — bring it, then update; "
                    "nothing was updated")
    return None


def _install_remote(root, tar: Path, log) -> None:
    from mmw.site import ssh
    log(f"updating mmw on {root.info.location}…")
    ssh.install_remote(root.host, tar)


def _install_here(tar: Path, log) -> None:
    prefix = U.installer_prefix()
    if prefix is None:
        raise ValueError("this mmw wasn't installed by mmw's installer — update it the way it was installed")
    log(f"updating mmw here ({prefix})…")
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(tar) as t:
            t.extractall(tmp, filter="data")
        script = next(Path(tmp).glob("mmw-*/install.sh"))
        r = subprocess.run(["sh", str(script), "--prefix", str(prefix)], capture_output=True, text=True)
        for line in (r.stdout + r.stderr).splitlines():
            if line.strip():
                log(f"  {line}")
        if r.returncode != 0:
            raise ValueError(f"install.sh failed (exit {r.returncode}) — still on {U.current_version()}")


def apply_far_update(far: FarOffer, log) -> None:
    """Every ssh root first, then this machine: a failure part-way leaves this
    machine on the old mmw, which every root still accepts."""
    problem = preflight(far)
    if problem:
        raise ValueError(problem)
    for root in far.remotes:
        _install_remote(root, far.installers[_arch_of(root)], log)
    _install_here(far.installers[machine_arch()], log)
