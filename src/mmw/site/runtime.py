"""A site's own mmw, updated from the runtime a bundle brings.

The bundle's `.mmw/runtime/` arrives in `{home}/runtime/incoming.tmp/`
during the apply. Once the apply is done, with the mmw that started it, a
newer runtime is installed beside the current one by its own bootstrap,
which switches `{home}/bin/mmw` only when the install completes.
"""
import json
import subprocess
from pathlib import Path

from mmw.core import disk_io

PREFIX = ".mmw/runtime/"


def incoming_dir(home: Path) -> Path:
    return Path(home) / "runtime" / "incoming.tmp"


def member_path(incoming: Path, name: str) -> Path:
    """Where a `.mmw/runtime/…` member goes; a name that would climb out
    is refused."""
    rel = Path(name[len(PREFIX):])
    if rel.is_absolute() or ".." in rel.parts or not rel.parts:
        raise ValueError(f"bundle is malformed: {name!r} isn't a runtime file")
    return incoming / rel


def machine_arch() -> str:
    import platform
    return {"arm64": "aarch64"}.get(platform.machine(), platform.machine())


def installed(home: Path) -> tuple[str, str] | None:
    """(version, python) of the runtime `bin/mmw` runs, from `runtime/CURRENT`."""
    try:
        version, python = disk_io.ops.read_text(Path(home) / "runtime" / "CURRENT").split()[:2]
        return version, python
    except (OSError, ValueError):
        return None


def update_runtime(home: Path, incoming: Path, *, allow_downgrade: bool = False) -> list[str]:
    """Install the runtime in `incoming` if it is newer than both this mmw
    and the one `bin/mmw` runs (or the same version on another Python; an
    older one only when `allow_downgrade`). Returns what to tell the
    operator; never raises — the apply it follows has already succeeded."""
    from packaging.version import InvalidVersion, Version

    from mmw.core.runtime import mmw_version, verify
    home = Path(home).absolute()
    running, inst = mmw_version(), installed(home)
    arch = machine_arch()
    try:
        top = json.loads(disk_io.ops.read_text(incoming / "RUNTIME.json"))
        new, python, arches = top["mmw"], top["python"], list(top["arches"])
        current = max(Version(running), Version(inst[0])) if inst else Version(running)
        newer, older = Version(new) > current, Version(new) < current
    except (OSError, ValueError, KeyError, TypeError, InvalidVersion) as e:
        disk_io.ops.rmtree(incoming, ignore_errors=True)
        return [f"the bundle's mmw runtime can't be read ({e}) — not installed"]
    other_python = inst is not None and Version(inst[0]) == Version(new) and inst[1] != python
    if not newer and not older and not other_python:
        disk_io.ops.rmtree(incoming, ignore_errors=True)
        return []
    if older and not allow_downgrade:
        disk_io.ops.rmtree(incoming, ignore_errors=True)
        return [f"this bundle brings mmw {new}, older than this mmw {current} — not installed "
                "(mmw site apply --allow-downgrade installs it)"]
    if arch not in arches:
        disk_io.ops.rmtree(incoming, ignore_errors=True)
        return [f"this bundle's mmw {new} is for {', '.join(arches)} — this machine is {arch}; not installed"]
    if verify(incoming / arch) is None:
        disk_io.ops.rmtree(incoming, ignore_errors=True)
        return [f"this bundle's mmw {new} runtime is damaged (its files don't match RUNTIME.json) — "
                "not installed; copy the bundle again"]
    final = incoming.with_name("incoming")
    if disk_io.ops.exists(final):
        disk_io.ops.rmtree(final)
    disk_io.ops.rename(incoming, final)
    try:
        r = subprocess.run(["/bin/sh", str(final / "bootstrap.sh"), "--home", str(home), "--from", str(final)],
                           capture_output=True, text=True, timeout=3600)
        ok, why = r.returncode == 0, (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1]
    except (OSError, subprocess.SubprocessError) as e:
        ok, why = False, str(e)
    if not ok:
        return [f"mmw {new} didn't install ({why}) — still running {running}; its runtime is in {final}"]
    notes = [f"mmw updated to {new} — restart it ({home}/bin/mmw)"]
    others = [a for a in arches if a != arch]
    if others:
        # Machines of another architecture sharing this home install their own.
        notes.append(f"on {', '.join(others)} machines sharing this home: sh {final}/bootstrap.sh --home {home}")
    else:
        disk_io.ops.rmtree(final, ignore_errors=True)
    return notes
