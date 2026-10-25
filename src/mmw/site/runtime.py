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


def update_runtime(home: Path, incoming: Path, *, allow_downgrade: bool = False) -> list[str]:
    """Install the runtime in `incoming` if it is newer than this mmw (or
    older, when `allow_downgrade`). Returns what to tell the operator; never
    raises — the apply it follows has already succeeded."""
    from packaging.version import InvalidVersion, Version

    from mmw.core.runtime import mmw_version
    current = mmw_version()
    try:
        new = json.loads(disk_io.ops.read_text(incoming / "RUNTIME.json"))["mmw"]
        newer, older = Version(new) > Version(current), Version(new) < Version(current)
    except (OSError, ValueError, KeyError, TypeError, InvalidVersion) as e:
        disk_io.ops.rmtree(incoming, ignore_errors=True)
        return [f"the bundle's mmw runtime can't be read ({e}) — not installed"]
    if not newer and not older:
        disk_io.ops.rmtree(incoming, ignore_errors=True)
        return []
    if older and not allow_downgrade:
        disk_io.ops.rmtree(incoming, ignore_errors=True)
        return [f"this bundle brings mmw {new}, older than this mmw {current} — not installed "
                "(mmw site apply --allow-downgrade installs it)"]
    final = incoming.with_name("incoming")
    if disk_io.ops.exists(final):
        disk_io.ops.rmtree(final)
    disk_io.ops.rename(incoming, final)
    home = Path(home).absolute()
    try:
        r = subprocess.run(["/bin/sh", str(final / "bootstrap.sh"), "--home", str(home), "--from", str(final)],
                           capture_output=True, text=True, timeout=3600)
        ok, why = r.returncode == 0, (r.stderr.strip().splitlines() or [f"exit {r.returncode}"])[-1]
    except (OSError, subprocess.SubprocessError) as e:
        ok, why = False, str(e)
    if not ok:
        return [f"mmw {new} didn't install ({why}) — still running {current}; its runtime is in {final}"]
    disk_io.ops.rmtree(final, ignore_errors=True)
    return [f"mmw updated to {new} — restart it"]
