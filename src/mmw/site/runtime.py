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


def installed(home: Path) -> tuple[str, str, str | None] | None:
    """(version, python, install name) of the runtime `bin/mmw` runs, from
    `runtime/CURRENT`."""
    try:
        fields = disk_io.ops.read_text(Path(home) / "runtime" / "CURRENT").split()
        return fields[0], fields[1], fields[2] if len(fields) > 2 else None
    except (OSError, IndexError):
        return None


def _other_build(home: Path, inst, incoming: Path, arch: str) -> bool:
    """The bundle's mmw wheel isn't the one installed (a rebuild of the same
    version)."""
    if inst is None or inst[2] is None:
        return False
    mine = Path(home) / "runtime" / f"{inst[2]}-{arch}" / "mmw.whl"
    theirs = next(iter(sorted(disk_io.ops.glob(incoming / arch / "wheels", "mirror_mirror_on_the_wall-*.whl"))), None)
    try:
        return theirs is not None and disk_io.ops.read_bytes(mine) != disk_io.ops.read_bytes(theirs)
    except OSError:
        return False


def decide(home: Path, top: dict, *, allow_downgrade: bool = False,
           incoming: Path | None = None) -> tuple[str, str | None]:
    """What to do with a bundle's runtime (its top `RUNTIME.json`):
    ("install", what), ("skip", None) when this site already runs it, or
    ("refuse", why). `incoming`, when the files are here, also tells a
    rebuilt wheel of the same version apart."""
    from packaging.version import InvalidVersion, Version

    from mmw.core.runtime import mmw_version
    running, inst = mmw_version(), installed(home)
    try:
        new, python = top["mmw"], top["python"]
        current = max(Version(running), Version(inst[0])) if inst else Version(running)
        newer, older = Version(new) > current, Version(new) < current
    except (KeyError, TypeError, InvalidVersion) as e:
        return "refuse", f"the bundle's mmw runtime can't be read ({top.get('error', e)}) — not installed"
    same = inst is not None and Version(inst[0]) == Version(new)
    other = same and (inst[1] != python or
                      (incoming is not None and _other_build(home, inst, incoming, machine_arch())))
    if older and not allow_downgrade:
        return "refuse", (f"this bundle brings mmw {new}, older than this mmw {current} — not installed "
                          "(mmw site apply --allow-downgrade installs it)")
    if newer or older or other:
        return "install", f"mmw {new} would be installed (now {current})"
    return "skip", None


def update_runtime(home: Path, incoming: Path, *, allow_downgrade: bool = False) -> list[str]:
    """Install the runtime in `incoming` if it is newer than both this mmw
    and the one `bin/mmw` runs (or the same version on another Python, or
    rebuilt; an older one only when `allow_downgrade`). Returns what to tell the
    operator; never raises — the apply it follows has already succeeded."""
    from mmw.core.runtime import mmw_version, verify
    home = Path(home).absolute()
    running = mmw_version()
    arch = machine_arch()
    try:
        top = json.loads(disk_io.ops.read_text(incoming / "RUNTIME.json"))
    except (OSError, ValueError) as e:
        top = {"error": str(e)}
    verdict, note = decide(home, top, allow_downgrade=allow_downgrade, incoming=incoming)
    if verdict != "install":
        disk_io.ops.rmtree(incoming, ignore_errors=True)
        return [note] if note else []
    new, arches = top["mmw"], list(top["arches"])
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
