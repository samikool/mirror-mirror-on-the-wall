"""One operator at a time: a lock file in the site's home, created
exclusively and holding who took it. SQLite's own locking is not trusted
over NFS, so every command that changes the site takes this first."""
import os
import socket
import time
from pathlib import Path

from mmw.core import disk_io

LOCK = "lock"


class LockHeld(Exception):
    def __init__(self, holder: str):
        super().__init__(f"the site is in use by {holder} — wait for it, or `--break-lock` "
                         "if that run is gone")
        self.holder = holder


class SiteLock:
    def __init__(self, home: Path):
        self.path = Path(home) / LOCK

    def __enter__(self) -> "SiteLock":
        try:
            with disk_io.ops.open_file(self.path, "x") as fh:
                fh.write(f"{socket.gethostname()} {os.getpid()} "
                         f"{time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
        except FileExistsError:
            try:
                holder = disk_io.ops.read_text(self.path).strip()
            except OSError:
                holder = "another run"
            raise LockHeld(holder) from None
        return self

    def __exit__(self, *exc) -> None:
        disk_io.ops.unlink(self.path, missing_ok=True)


def break_lock(home: Path) -> bool:
    """Remove a stale lock; False when there was none."""
    return disk_io.ops.remove(Path(home) / LOCK) is not None
