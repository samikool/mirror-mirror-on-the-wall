"""Move the pool database to another path — typically a fast disk.

Under the pool's exclusive lock (no sync may write meanwhile): fold the WAL
into the main file, copy it beside its destination, check the copy is
byte-for-byte the original, swap it into place, save `settings.database`, and
only then remove the old file and its -wal/-shm. Any failure before the save
leaves the old database and mmw.toml exactly as they were.
"""
import hashlib
import os
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

from mmw.core import config
from mmw.core.pool import PoolBusy, exclusive_pool_lock, register_database

_CHUNK = 8 << 20


class DatabaseMoveError(RuntimeError):
    """The move did not happen; the old database is still the one in use."""


def _copy(src: Path, dst: Path, progress: Callable[[int, int], None] | None) -> None:
    total = src.stat().st_size
    done = 0
    buf = bytearray(_CHUNK)
    view = memoryview(buf)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while n := fin.readinto(buf):
            fout.write(view[:n])
            done += n
            if progress:
                progress(done, total)
        fout.flush()
        os.fsync(fout.fileno())


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


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
                  progress: Callable[[int, int], None] | None = None) -> config.Config:
    """Move `settings.database` to `dest`; returns the saved new config."""
    mirror_dir = cfg.settings.mirror_dir
    src = Path(cfg.settings.database)
    dest = Path(dest).expanduser()
    if not src.exists():
        raise DatabaseMoveError(f"there is no database at {src} to move")
    if dest.exists():
        raise DatabaseMoveError(f"{dest} already exists — choose a path for a new file")
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with exclusive_pool_lock(mirror_dir, "move the pool database"):
            _fold_wal(src)
            dest.parent.mkdir(parents=True, exist_ok=True)
            size = src.stat().st_size
            free = shutil.disk_usage(dest.parent).free
            if free < size:
                raise DatabaseMoveError(f"{dest.parent} has {free:,} bytes free; "
                                        f"the database needs {size:,}")
            saved = False
            try:
                _copy(src, tmp, progress)
                if _sha256(src) != _sha256(tmp):
                    raise DatabaseMoveError("the copy does not match the original")
                os.replace(tmp, dest)
                new = cfg.model_copy(update={
                    "settings": cfg.settings.model_copy(update={"database": dest})})
                config.save(new, cfg_dir)
                saved = True
            finally:
                if not saved:
                    tmp.unlink(missing_ok=True)
                    dest.unlink(missing_ok=True)
            register_database(mirror_dir, dest)
            for suffix in ("", "-wal", "-shm"):
                Path(f"{src}{suffix}").unlink(missing_ok=True)
            return new
    except PoolBusy:
        raise DatabaseMoveError("a sync, publish or bundle is using the pool — wait for "
                                "it to finish, then move the database") from None
    except OSError as e:
        raise DatabaseMoveError(str(e)) from e
