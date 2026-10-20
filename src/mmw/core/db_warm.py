"""Read the pool database into the page cache before a heavy job.

On a spinning disk the listings of a big network pay one seek per page they
touch; one sequential pass first turns that into a single streaming read.
Only the kernel's page cache is filled — reclaimable, so this cannot OOM the
machine — never SQLite's cache or an in-memory copy. Skipped only when the
file would not fit (over 90 % of MemAvailable): then its later pages would
just evict its earlier ones. Any trouble means "skip", never a
failed job.
"""
import ctypes
import ctypes.util
import mmap
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from mmw.core import disk_io

_MEMINFO = Path("/proc/meminfo")
_CHUNK = 8 << 20


@dataclass
class WarmResult:
    status: Literal["resident", "warmed", "too-big", "stopped", "unavailable"]
    resident_before: float
    seconds: float
    message: str


def _available_bytes() -> int | None:
    try:
        for line in _MEMINFO.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def resident_fraction(path: Path) -> float:
    """Share of the file's pages in the page cache (mincore); 0.0 if unknown."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        libc.mmap.restype = ctypes.c_void_p
        libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, ctypes.c_long]
        libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
        libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p]
        size = disk_io.ops.stat(path).st_size
        if size == 0:
            return 1.0
        page = os.sysconf("SC_PAGE_SIZE")
        pages = (size + page - 1) // page
        fd = disk_io.ops.open_fd(path, os.O_RDONLY)
        try:
            addr = libc.mmap(None, size, mmap.PROT_READ, mmap.MAP_SHARED, fd, 0)
            if addr in (None, ctypes.c_void_p(-1).value):
                return 0.0
            try:
                vec = (ctypes.c_ubyte * pages)()
                if libc.mincore(ctypes.c_void_p(addr), size, vec) != 0:
                    return 0.0
                return sum(b & 1 for b in vec) / pages
            finally:
                libc.munmap(ctypes.c_void_p(addr), size)
        finally:
            os.close(fd)
    except (OSError, AttributeError, TypeError, ValueError):
        return 0.0


def warm_database(path: Path, *, stop_event=None, progress=None,
                  mem_fraction: float = 0.9, resident_skip: float = 0.95) -> WarmResult:
    started = time.perf_counter()

    def done(status, before, message):
        return WarmResult(status, before, time.perf_counter() - started, message)
    path = Path(path)
    try:
        size = disk_io.ops.stat(path).st_size
    except OSError:
        return done("unavailable", 0.0, "")      # a first sync: nothing to warm, nothing to say
    before = resident_fraction(path)
    if before >= resident_skip:
        return done("resident", before, "pool database already in memory")
    available = _available_bytes()
    if available is None:
        return done("unavailable", before, "available memory unknown — warming skipped")
    if size > mem_fraction * available:
        return done("too-big", before,
                    f"pool database ({size >> 20:,} MiB) is over {mem_fraction:.0%} of available "
                    f"memory ({available >> 20:,} MiB) — warming skipped")
    if progress:
        progress(f"warming the pool database ({size >> 20:,} MiB)…")
    try:
        if not disk_io.ops.read_through(path, _CHUNK, stop_event):
            return done("stopped", before, "warming stopped")
    except OSError as e:
        return done("unavailable", before, f"could not read the database ({e}) — warming skipped")
    return done("warmed", before,
                f"pool database warmed ({size >> 20:,} MiB in {time.perf_counter() - started:.1f}s)")


def warm_for_job(mirror_dir: Path, *, say=None, stop_event=None) -> WarmResult:
    """Warm this pool's configured database at the start of a heavy job
    (sync, publish, bundle, verify) and report one line through `say`."""
    from mmw.core.pool import database_path
    result = warm_database(database_path(mirror_dir), stop_event=stop_event, progress=say)
    if say and result.message:
        say(result.message)
    return result
