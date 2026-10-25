"""What this machine remembers about mirrors: the root locations it has
opened, and where the CA is. Conveniences only — never the source of truth;
deleting them loses nothing a root doesn't hold itself."""
import json
import os
from pathlib import Path

from mmw.core import disk_io


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "mmw"


def _read(name: str, default):
    path = state_dir() / name
    try:
        return json.loads(disk_io.ops.read_text(path))
    except (OSError, ValueError):
        return default


def _write(name: str, data) -> None:
    disk_io.ops.mkdir(state_dir(), parents=True, exist_ok=True)
    disk_io.ops.atomic_write(state_dir() / name, (json.dumps(data, indent=2) + "\n").encode())


def known() -> list[str]:
    """Root locations this machine has opened, the most recent first."""
    got = _read("roots.json", [])
    return [x for x in got if isinstance(x, str)] if isinstance(got, list) else []


def remember(location: str) -> None:
    _write("roots.json", [location] + [x for x in known() if x != location])


def forget(location: str) -> None:
    _write("roots.json", [x for x in known() if x != location])


def ca_paths() -> tuple[Path, Path] | None:
    """Where the CA is (by path only: the key is never copied, its passphrase never kept)."""
    got = _read("ca.json", None)
    if isinstance(got, dict) and got.get("cert") and got.get("key"):
        return Path(got["cert"]), Path(got["key"])
    return None


def set_ca_paths(cert: Path, key: Path) -> None:
    _write("ca.json", {"cert": str(cert), "key": str(key)})
