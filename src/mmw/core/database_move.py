"""Move the pool database to another path — typically a fast disk.

Under the pool's exclusive lock (no sync may write meanwhile): fold the WAL
into the main file, copy it beside its destination, check the copy is
byte-for-byte the original, swap it into place, save `settings.database`, and
only then remove the old file and its -wal/-shm. Any failure before the save
leaves the old database and mmw.toml exactly as they were.
"""
import os
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

from mmw.core import config, disk_io
from mmw.core.pool import PoolBusy, exclusive_pool_lock, register_database

_CHUNK = 8 << 20


class DatabaseMoveError(RuntimeError):
    """The move did not happen; the old database is still the one in use."""


def _copy(src: Path, dst: Path, progress: Callable[[int, int], None] | None) -> None:
    disk_io.ops.copy_stream(src, dst, progress, chunk=_CHUNK)


def _sha256(path: Path) -> str:
    return disk_io.hash_file(path)


def _fsync_dir(directory: Path) -> None:
    """Make a rename in `directory` durable (the file's own fsync is not)."""
    fd = disk_io.ops.open_fd(directory, os.O_RDONLY)
    try:
        disk_io.ops.fsync(fd)
    finally:
        os.close(fd)


def _remove_old(src: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        disk_io.ops.unlink(Path(f"{src}{suffix}"), missing_ok=True)


def _fold_wal(db: Path) -> None:
    conn = sqlite3.connect(db)
    try:
        busy, _log, _done = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        conn.close()
    if busy:
        raise DatabaseMoveError("the pool database is in use — close other mmw windows "
                                "and jobs, then move it")


def move_database(cfg_dir: Path, cfg: config.Config, dest: Path, *,
                  progress: Callable[[int, int], None] | None = None,
                  warnings: list[str] | None = None) -> config.Config:
    """Move `settings.database` to `dest`; returns the saved new config.

    Once mmw.toml points at the new file the move has happened: trouble
    removing the old one after that is a note in `warnings`, not a failure."""
    mirror_dir = cfg.settings.mirror_dir
    src = Path(cfg.settings.database)
    # Absolute: a path relative to wherever the command ran means nothing
    # to the next mmw run.
    dest = Path(dest).expanduser().resolve()
    if not disk_io.ops.exists(src):
        raise DatabaseMoveError(f"there is no database at {src} to move")
    if disk_io.ops.exists(dest):
        raise DatabaseMoveError(f"{dest} already exists — choose a path for a new file")
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with exclusive_pool_lock(mirror_dir, "move the pool database"):
            _fold_wal(src)
            made = [p for p in (dest.parent, *dest.parent.parents) if not disk_io.ops.exists(p)]
            saved = False
            try:
                disk_io.ops.mkdir(dest.parent, parents=True, exist_ok=True)
                size = disk_io.ops.stat(src).st_size
                free = shutil.disk_usage(dest.parent).free
                if free < size:
                    raise DatabaseMoveError(f"{dest.parent} has {free:,} bytes free; "
                                            f"the database needs {size:,}")
                _copy(src, tmp, progress)
                if _sha256(src) != _sha256(tmp):
                    raise DatabaseMoveError("the copy does not match the original")
                disk_io.ops.replace(tmp, dest)
                _fsync_dir(dest.parent)
                new = cfg.model_copy(update={
                    "settings": cfg.settings.model_copy(update={"database": dest})})
                config.save(new, cfg_dir)
                saved = True
            finally:
                if not saved:
                    disk_io.ops.unlink(tmp, missing_ok=True)
                    disk_io.ops.unlink(dest, missing_ok=True)
                    for d in made:                     # deepest first
                        try:
                            disk_io.ops.rmdir(d)
                        except OSError:
                            break
            register_database(mirror_dir, dest)
            try:
                _remove_old(src)
            except OSError as e:
                if warnings is not None:
                    warnings.append(f"the old database at {src} could not be "
                                    f"removed ({e.strerror or e}) — delete it yourself")
            return new
    except PoolBusy:
        raise DatabaseMoveError("a sync, publish or bundle is using the pool — wait for "
                                "it to finish, then move the database") from None
    except OSError as e:
        raise DatabaseMoveError(str(e)) from e
