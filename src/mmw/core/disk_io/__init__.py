"""All disk I/O behind one layer.

Callers state what they need ("place these files", "stat these blobs",
"remove these paths", "read these files"); a `DiskJob` routes each request to
the way of talking to the disk that suits it:

- **write-meta** — hardlink/place, unlink: a per-job lane of
  `disk_write_threads` workers.
- **read-meta** — stat, small whole-file reads: a per-job lane of
  `disk_read_threads` workers.
- **stream** — hashing, a bundle's tar payload: one ordered consumer on the
  caller's thread, with the next `disk_readahead` files opened ahead on the
  read-meta lane. Never parallel reads: the HDD head would seek between files
  (inference; measure before changing).

Lanes are per job, never process-wide: two jobs never queue on each other.
Every count is an `mmw.toml` setting, tuned on one HDD; `1 / 1 / 0` is
exactly the serial behaviour mmw had before this layer. See
docs/internals/superpowers/specs/2026-10-20-disk-io-layer-design.md.
"""
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from mmw.core.disk_io import ops
from mmw.core.disk_io.lane import Lane
from mmw.core.disk_io.stats import DiskStats, RouteStats

__all__ = ["DiskJob", "DiskStats", "Lane", "Limits", "RouteStats", "configure", "hash_file",
           "limits", "ops", "read_files"]

_READ_REPORT_EVERY = 5000


@dataclass(frozen=True)
class Limits:
    write_threads: int = 8
    read_threads: int = 16
    readahead: int = 16


_limits = Limits()
# Set by the test suite: the suite runs at 1 / 1 / 0, and loading a config
# must not change that under it.
_pinned = False


def configure(settings) -> None:
    """Adopt `settings`' disk counts for jobs started from now on. A job
    already running keeps the values it started with."""
    global _limits
    if _pinned:
        return
    _limits = Limits(settings.disk_write_threads, settings.disk_read_threads,
                     settings.disk_readahead)


def limits() -> Limits:
    return _limits


class DiskJob:
    """One heavy job's disk I/O: a sync of one mirror, a publish, a bundle, a
    verify, a prune, a snapshot. Owns its lanes (made lazily, sized from
    `limits()` at construction), its stop_event and its DiskStats. A context
    manager; close() shuts its lanes down."""

    def __init__(self, name: str, *, stop_event=None, limits: Limits | None = None):
        self.name = name
        self.stop_event = stop_event
        self.limits = limits or _limits
        self.stats = DiskStats()
        self._write: Lane | None = None
        self._read: Lane | None = None

    def __enter__(self) -> "DiskJob":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        for lane in (self._write, self._read):
            if lane is not None:
                lane.close()
        self._write = self._read = None

    def _write_lane(self) -> Lane:
        if self._write is None:
            self._write = Lane(self.limits.write_threads, f"{self.name}-write", self.stats, "write-meta")
        return self._write

    def _read_lane(self) -> Lane:
        if self._read is None:
            self._read = Lane(self.limits.read_threads, f"{self.name}-read", self.stats, "read-meta")
        return self._read

    # ── write-meta ──────────────────────────────────────────────────────────

    def place(self, pairs, *, tmp_dir: Path):
        """(src, dst) → yields ((src, dst), "linked" | "skipped" | Exception)
        in order. Same inode → skipped; staged in tmp_dir, then os.replace."""
        return self.meta_map(lambda pair: ops.place(pair[0], pair[1], tmp_dir), pairs)

    def remove(self, paths):
        """→ yields (path, size_freed | None | Exception) in order.
        FileNotFoundError → None."""
        return self.meta_map(ops.remove, paths)

    def meta_map(self, fn, items):
        """The escape hatch for compound per-item metadata work (e.g.
        TreeSink._place's exists + same_file + place). Write-meta lane."""
        return self._write_lane().ordered_map(fn, items, stop_event=self.stop_event)

    # ── read-meta ───────────────────────────────────────────────────────────

    def stat(self, paths):
        """→ yields (path, os.stat_result | Exception) in order."""
        return self.read_map(ops.stat, paths)

    def read_small(self, paths):
        """→ yields (path, bytes | Exception) in order."""
        for path, body in self.read_map(lambda p: ops.read_bytes(p), paths):
            if isinstance(body, bytes):
                self.stats.add("read-meta", 0, 0.0, len(body))
            yield path, body

    def read_map(self, fn, items):
        """Escape hatch on the read-meta lane."""
        return self._read_lane().ordered_map(fn, items, stop_event=self.stop_event)

    # ── write-data ──────────────────────────────────────────────────────────

    def atomic_write(self, dst: Path, data: bytes, *, tmp_dir: Path | None = None) -> None:
        """`ops.atomic_write` on the caller's thread, timed into write-data."""
        started = time.perf_counter()
        ops.atomic_write(dst, data, tmp_dir)
        self.stats.add("write-data", 1, time.perf_counter() - started, len(data))

    # ── stream ──────────────────────────────────────────────────────────────

    def stream_open(self, paths):
        """→ yields (path, open binary file | Exception) in order. The caller
        owns each handle it receives and closes it. The opens run up to
        `readahead` ahead on the read-meta lane; data reads happen on the
        caller's thread. Handles opened but never handed over are closed
        when the generator closes. readahead == 0 (or a one-thread read
        lane) → open inline."""
        ops_done, waited = 0, 0.0
        try:
            if self.limits.readahead == 0:
                for path in paths:
                    if self.stop_event is not None and self.stop_event.is_set():
                        return
                    started = time.perf_counter()
                    try:
                        fh = ops.open_rb(path)
                    except Exception as e:
                        fh = e
                    waited += time.perf_counter() - started
                    ops_done += 1
                    yield path, fh
                return
            lock, held, abandoned = threading.Lock(), {}, [False]

            def open_ahead(path):
                fh = ops.open_rb(path)
                with lock:
                    if abandoned[0]:          # the consumer has gone: never hand it over
                        fh.close()
                        raise _Abandoned
                    held[id(fh)] = fh
                return fh

            opened = self._read_lane().ordered_map(open_ahead, paths, stop_event=self.stop_event,
                                                   window=self.limits.readahead)
            try:
                for path, fh in opened:
                    if not isinstance(fh, Exception):
                        with lock:
                            held.pop(id(fh), None)
                    ops_done += 1
                    yield path, fh
            finally:
                opened.close()
                with lock:
                    abandoned[0] = True
                    for fh in held.values():
                        fh.close()
                    held.clear()
        finally:
            # Opens ahead are timed by the read lane (what the caller waited
            # on them); data reads by whoever reads (`hash_files`).
            self.stats.add("stream", ops_done, waited)

    def hash_files(self, paths, algo: str = "sha256"):
        """→ yields (path, hexdigest | Exception) in order. Sequential reads
        in 1 MiB chunks on the caller's thread, the opens via stream_open."""
        for path, fh in self.stream_open(paths):
            if isinstance(fh, Exception):
                yield path, fh
                continue
            started = time.perf_counter()
            try:
                with fh:
                    digest, nbytes = ops.hash_fh(fh, algo)
            except OSError as e:
                yield path, e
                continue
            self.stats.add("stream", 0, time.perf_counter() - started, nbytes)
            yield path, digest


class _Abandoned(Exception):
    """An open-ahead that finished after its stream was closed."""


def read_files(paths, *, job: DiskJob | None = None, on_progress=None) -> dict:
    """{path: its bytes, or the OSError reading it raised} for every distinct
    path — many small files (a VS Code mirror's 120k metadata JSONs) read on
    the job's read lane. One at a time, each costs a full seek on a spinning
    disk; a queue of them the drive reorders, several times faster (16
    workers measured best; 32 left the TUI's thread waiting its turn).

    `on_progress(done, total)` fires every few thousand files. A stop on the
    job's `stop_event` returns what was read so far — the caller checks it.
    """
    paths = list(dict.fromkeys(paths))
    own = job is None
    job = job or DiskJob("read")
    try:
        out: dict = {}
        for path, body in job.read_small(paths):
            if isinstance(body, Exception) and not isinstance(body, OSError):
                raise body
            out[path] = body
            if on_progress and len(out) % _READ_REPORT_EVERY == 0:
                on_progress(len(out), len(paths))
        return out
    finally:
        if own:
            job.close()


def hash_file(path, algo: str = "sha256") -> str:
    """The hexdigest of one file, streamed — for one-off callers outside a job."""
    with ops.open_rb(path) as fh:
        return ops.hash_fh(fh, algo)[0]
