"""How much room a root really has.

Inside WSL the Linux filesystem is a virtual disk that reports about 1 TB
free whatever the Windows drive behind it holds, so a check against it alone
passes and the Windows drive fills up (WSL then breaks). For a root inside
WSL's own filesystem, the Windows drive's free space counts too — the smaller
is the truth. A root on a Windows drive (/mnt/c, /mnt/d, …) is that drive."""
import functools
from pathlib import Path

from mmw.core import disk_io

WINDOWS_DRIVE = Path("/mnt/c")          # where WSL keeps its virtual disk by default


@functools.lru_cache(maxsize=1)
def _wsl() -> bool:
    try:
        return "microsoft" in disk_io.ops.read_text(Path("/proc/sys/kernel/osrelease")).lower()
    except OSError:
        return False


def _existing(path: Path) -> Path:
    path = Path(path).expanduser()
    while not disk_io.ops.exists(path) and path != path.parent:
        path = path.parent
    return path


def _windows_drive_free() -> int | None:
    if not disk_io.ops.exists(WINDOWS_DRIVE):
        return None
    return disk_io.ops.disk_usage(WINDOWS_DRIVE).free


def real_free(path) -> int:
    path = _existing(Path(path))
    free = disk_io.ops.disk_usage(path).free
    if _wsl() and not str(path.resolve()).startswith("/mnt/"):
        windows = _windows_drive_free()
        if windows is not None:
            free = min(free, windows)
    return free
