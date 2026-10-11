import fcntl
import hashlib
import os
import queue
import re
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


# A blob's filesystem name is `{checksum[:2]}/{checksum}-{filename}`, so both
# halves are path components. `filename` is already reduced to a basename by
# every caller, but `checksum` is not always locally computed: a *package*
# checksum is hashed from the bytes we just downloaded (safe), while a
# *metadata* checksum is read verbatim out of the upstream Release SHA256
# block, where only `int(size)` was ever validated. A Release line reading
# `../../../../srv/lab/evil 9 main/binary-amd64/Packages` therefore chose the
# write path: httpx normalises the dot segments in the by-hash URL, the
# by-hash fetch deliberately skips the hash check ("content IS the hash"), and
# `mkdir(parents=True)` + `os.link()` did the rest. Verified writing outside
# `mirror_dir`. Anything that is not a plain hex digest is refused here, at
# the store, rather than relying on every producer to be careful.
_HEX_DIGEST_RE = re.compile(r"\A[0-9a-f]{32,128}\Z")


def is_safe_checksum(checksum: str) -> bool:
    """True when `checksum` is a lowercase hex digest of a plausible length.

    md5/sha1/sha256/sha512 are 32/40/64/128 hex characters. Upper case is
    rejected on purpose: `hashlib.hexdigest()` is lowercase, so a mixed-case
    value did not come from us and normalising it would hide that.
    """
    return bool(_HEX_DIGEST_RE.match(checksum))


def _validate_blob_name(checksum: str, filename: str) -> None:
    if not is_safe_checksum(checksum):
        raise ValueError(
            f"refusing to pool a blob under a checksum that is not a hex digest: "
            f"{checksum!r} — upstream metadata cannot choose a filesystem path"
        )
    if (
        not filename
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
        or filename in (".", "..")
        or Path(filename).name != filename
    ):
        raise ValueError(
            f"refusing to pool a blob under a filename carrying a path: {filename!r}"
        )


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS packages (
    checksum      TEXT PRIMARY KEY,
    checksum_type TEXT NOT NULL,
    filename      TEXT NOT NULL,
    size          INTEGER NOT NULL,
    pool_path     TEXT NOT NULL,
    added_at      TEXT DEFAULT (datetime('now'))
);

-- `SELECT COUNT(*), SUM(size) FROM packages` is the pool-size stat every
-- frontend shows. Without an index it is a full table scan: measured at 0.4 s
-- on a compacted 320k-row pool and ~2 s on the live one. A covering index on
-- `size` turns it into an index-only scan — measured **26x faster (0.014 s)**.
-- `size` is an original column, so this is safe inside SCHEMA; indexes on
-- ALTER-added columns must be created after the ALTER instead.
CREATE INDEX IF NOT EXISTS idx_packages_size ON packages(size);

CREATE TABLE IF NOT EXISTS refs (
    checksum     TEXT NOT NULL,
    mirror_name  TEXT NOT NULL,
    repo_path    TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'pkg',  -- 'pkg' | 'meta' | 'release'
    -- Repository URI this mirror belongs to. Pool GC asks "is this ref's
    -- SOURCE still configured?"; asking "is this mirror_name still in the
    -- config?" deletes every arch the config does not happen to spell out,
    -- which is all of them once `arches = []` means "all".
    source       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (checksum, mirror_name, repo_path),
    FOREIGN KEY (checksum) REFERENCES packages(checksum)
);

-- Network snapshots: a network's complete published tree at a moment, kept
-- so it can be bundled to or published again exactly. Every checksum here is
-- a GC root, like `refs` — prune never deletes a file a snapshot still lists.
-- `complete = 0`: imported from a bundle manifest written before snapshots
-- existed; its file list is exact but some files may already be pruned, so
-- it can be a bundle's starting point but never its destination.
CREATE TABLE IF NOT EXISTS snapshots (
    id        INTEGER PRIMARY KEY,
    network   TEXT NOT NULL,
    stamp     TEXT NOT NULL,                 -- 20261011T120000Z, as apply.py uses
    label     TEXT,
    kind      TEXT NOT NULL,                 -- 'manual' | 'bundle' | 'imported'
    complete  INTEGER NOT NULL DEFAULT 1,
    settings  TEXT NOT NULL DEFAULT '{}',    -- what regenerated files need (JSON)
    UNIQUE (network, stamp)
);
-- Paths and checksums are stored once and shared: consecutive snapshots are
-- nearly identical, so a row is three integers. With the text inline, one
-- snapshot of a 514k-file network grew the database by 225 MB.
CREATE TABLE IF NOT EXISTS snapshot_paths (
    id    INTEGER PRIMARY KEY,
    path  TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS snapshot_blobs (
    id        INTEGER PRIMARY KEY,
    checksum  TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS snapshot_files (
    snapshot  INTEGER NOT NULL,
    path      INTEGER NOT NULL,              -- snapshot_paths.id
    blob      INTEGER NOT NULL,              -- snapshot_blobs.id
    PRIMARY KEY (snapshot, path)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_snapshot_files_blob ON snapshot_files(blob, snapshot);

-- Every bundle made: where it started and ended. "Last bundled to" — the
-- default starting point of the next bundle — is the newest row, which
-- after a backwards bundle (5 -> 3) is 3, not the newest snapshot.
CREATE TABLE IF NOT EXISTS bundles (
    id          INTEGER PRIMARY KEY,
    network     TEXT NOT NULL,
    from_stamp  TEXT,                         -- NULL: a full bundle
    to_stamp    TEXT NOT NULL,
    out_path    TEXT NOT NULL,
    sent        INTEGER NOT NULL DEFAULT 0,
    sent_bytes  INTEGER NOT NULL DEFAULT 0,
    removed     INTEGER NOT NULL DEFAULT 0,      -- paths the far side deletes
    made        TEXT NOT NULL DEFAULT (strftime('%Y%m%dT%H%M%SZ', 'now'))
);

-- What each publish destination serves: 'live' (the network's current state)
-- or a snapshot's stamp. A snapshot some root serves cannot be deleted.
CREATE TABLE IF NOT EXISTS published (
    root     TEXT PRIMARY KEY,                -- absolute directory
    network  TEXT NOT NULL,
    target   TEXT NOT NULL,                   -- 'live' | snapshot stamp
    made     TEXT NOT NULL DEFAULT (strftime('%Y%m%dT%H%M%SZ', 'now'))
);

-- mirror_stats / group_unique_count / ref_map all filter on mirror_name;
-- without this they full-scan refs on every detail-panel highlight.
CREATE INDEX IF NOT EXISTS idx_refs_mirror ON refs(mirror_name, checksum);

-- What each synced mirror IS. The pool is the authority on what exists:
-- architectures are discovered rather than declared, so config expansion is
-- narrower than reality and cannot be used to enumerate for publish.
CREATE TABLE IF NOT EXISTS mirrors (
    name            TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    provider        TEXT NOT NULL,
    source_url      TEXT NOT NULL,
    arch            TEXT NOT NULL DEFAULT '',
    suite           TEXT,
    component       TEXT,
    label           TEXT NOT NULL DEFAULT '',
    include_sources INTEGER NOT NULL DEFAULT 0,
    dist_upgrade    INTEGER NOT NULL DEFAULT 0
);
"""

_CHUNK = 900

# Reader tuning. This workload is **disk-bound**, not lock-bound: measured 1.4x
# from these two pragmas alone, while *adding read connections made it worse*
# (1 conn 350 q/s, 4 conns 190, 8 conns 107 — each connection keeps its own page
# cache, so the same hot pages get read from disk once per connection while the
# OS cache is already being thrashed by the mirror sync).
_READ_PRAGMAS = (
    "PRAGMA cache_size=-65536",          # 64 MB page cache (default is 2 MB)
    "PRAGMA mmap_size=1073741824",       # 1 GB of address space, not resident
)


def _tune_reads(conn: sqlite3.Connection) -> None:
    for pragma in _READ_PRAGMAS:
        try:
            conn.execute(pragma)
        except sqlite3.OperationalError:
            pass                          # older SQLite without mmap — harmless

# refs.kind values: desired-state entries reconciled from the upstream index.
KIND_PKG = "pkg"          # package payload
KIND_META = "meta"        # index files referenced by the release file
KIND_RELEASE = "release"  # trust root (InRelease / Release(.gpg) / repomd.xml(.asc)) — publish places these LAST

# Cross-process advisory lock. There was NO locking of any kind, so a cron
# `mmw prune` overlapping a running sync deleted every blob the sync had
# downloaded so far: refs are reconciled only at the END of a mirror, so for
# the whole download phase (hours, multi-TB) every fresh blob is
# `packages − refs` — exactly prune's definition of an orphan. The sync then
# reconciled refs naming checksums whose files had just been unlinked,
# producing refs-without-packages that publish silently omits and that surface
# as a 404 during `apt install` on the far side of an airgap.
#
# A sync holds this SHARED for the duration of each mirror; prune and wipe take
# it EXCLUSIVE and refuse rather than wait — a destructive operation should say
# "something else is using the pool" instead of blocking a cron slot for hours.
# flock is per open-file-description, so this also protects the TUI from
# pruning while its own sync is running, which is the same mistake.
_LOCK_FILE = "lock"

# In-flight downloads land here before being linked into packages/.
_TMP_DIR = "tmp"


class PoolBusy(RuntimeError):
    """Another process (or this one) holds the pool for a conflicting use."""


def _lock_path(mirror_dir: Path) -> Path:
    return mirror_dir / ".pool" / _LOCK_FILE


# The sync's shared lock is process-wide and reference counted, not per-call.
#
# It was originally taken inside `download_mirror` as a context manager, which
# was wrong in two ways at once and locked up a real 45-mirror sync:
#
#   * `download_mirror` is a coroutine. In the TUI it runs on the event loop
#     (`sync.py:_sync_mirror` is an async worker), so `mkdir` + `open` +
#     `flock` — three blocking filesystem calls — executed on the UI thread,
#     once per mirror. A profile of that run put the MainThread at 21.8% of
#     samples inside `shared_pool_lock`, top stack
#     `shared_pool_lock <- download_mirror <- sync_mirror <- _sync_mirror`.
#   * `flock(LOCK_SH)` has no `LOCK_NB`, so if anything did hold the exclusive
#     lock every one of those calls waited on the kernel, on the event loop,
#     forever.
#
# The fact being guarded — "a sync is running in this process" — is
# process-level, so one lock is taken for the whole run and the other mirrors
# hit a refcount. Acquisition still blocks (waiting for a prune to finish is
# the correct behaviour), so it MUST be called from a worker thread; the
# `acquire_sync_lock` docstring says so and `downloader` routes it through
# `run_in_executor`.
_SYNC_LOCK_MUTEX = threading.Lock()
_sync_lock_fh = None
_sync_lock_depth = 0


def acquire_sync_lock(mirror_dir: Path) -> None:
    """Take the shared pool lock for this process. **Blocking — never call
    this from the event loop.**

    Failing to take it is deliberately not fatal: a read-only filesystem or a
    mount without flock support must not stop a sync that would otherwise
    work. The protection is best-effort by design.
    """
    global _sync_lock_fh, _sync_lock_depth
    with _SYNC_LOCK_MUTEX:
        if _sync_lock_depth > 0:
            _sync_lock_depth += 1
            return
        fh = None
        try:
            path = _lock_path(mirror_dir)
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(path, "a+")
            # Blocking on purpose: if a prune or wipe holds the pool, the right
            # thing is to wait for it rather than race it. The mutex is held
            # across this, so concurrent callers queue behind the first — which
            # is fine, because while the pool is being pruned they have no
            # useful work to do anyway.
            fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
        except OSError:
            if fh is not None:
                fh.close()
            fh = None
        _sync_lock_fh = fh
        _sync_lock_depth = 1


def release_sync_lock() -> None:
    """Drop one reference; the lock is released when the last one goes."""
    global _sync_lock_fh, _sync_lock_depth
    with _SYNC_LOCK_MUTEX:
        if _sync_lock_depth == 0:
            return
        _sync_lock_depth -= 1
        if _sync_lock_depth == 0 and _sync_lock_fh is not None:
            try:
                fcntl.flock(_sync_lock_fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            finally:
                _sync_lock_fh.close()
                _sync_lock_fh = None


@contextmanager
def shared_pool_lock(mirror_dir: Path):
    """Context-manager form of `acquire_sync_lock`. Blocking — same rule."""
    acquire_sync_lock(mirror_dir)
    try:
        yield
    finally:
        release_sync_lock()


def pool_is_busy(mirror_dir: Path) -> bool:
    """True when a sync currently holds the pool.

    A probe, not a reservation: it takes the exclusive lock and drops it again
    immediately, so it answers "would a destructive operation be refused right
    now?" without holding anything. Callers that need the guarantee must still
    do the real work inside `exclusive_pool_lock` — this exists so a UI can
    refuse *before* opening a progress modal rather than after.
    """
    try:
        with exclusive_pool_lock(mirror_dir, "check the pool"):
            return False
    except PoolBusy:
        return True


@contextmanager
def exclusive_pool_lock(mirror_dir: Path, what: str = "operation"):
    """Take the pool exclusively for a destructive operation, or raise PoolBusy.

    Non-blocking on purpose — see `_LOCK_FILE`.
    """
    path = _lock_path(mirror_dir)
    fh = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(path, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if fh is not None:
            fh.close()
        raise PoolBusy(
            f"refusing to {what}: the pool at {mirror_dir} is in use by a running "
            f"sync. A prune during a sync deletes the blobs it has just "
            f"downloaded, because their refs are not committed until the mirror "
            f"finishes. Wait for the sync to end and try again."
        )
    except OSError:
        if fh is not None:
            fh.close()
        fh = None          # no flock support — proceed unprotected, as above
    try:
        yield
    finally:
        if fh is not None:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()
_TMP_MAX_AGE = 24 * 3600  # crash leftovers older than this are reaped at init


class _DeferredWriter:
    """Drains package-table rows on one thread, committing in batches.

    **Why this exists.** `Pool.add()` used to INSERT + `commit()` inline, under
    the pool's write mutex, on the *same executor thread that was streaming the
    HTTP body*. mmw has one thread pool (`max_connections`, 32) shared by
    downloads, metadata and pool writes, and `_do_one` holds a global semaphore
    permit across the whole executor call — so a thread parked on that mutex was
    a thread not reading from a socket, holding one of only 32 permits while
    doing no network at all.

    Profiled on a live sync: workers at **2.4% CPU** with **92% of stack samples
    inside `pool.add` / `pool._read`** — 32 idle sockets waiting on a lock.
    Measured ceiling ~1,000 adds/sec regardless of package size, which is why
    throughput fell as the sync reached ubuntu-ports' long tail (43% of blobs
    are under 50 KB: 1,000/s x 20 KB is 20 MB/s).

    Batching alone was 3.4x. Moving the write off the download thread is the
    structural half: the network stops waiting on the database at all.
    """

    _SENTINEL = object()

    class _Task:
        """A unit of work that must run *after* queued rows are committed —
        `reconcile_refs`, whose refs are joined against those rows."""

        __slots__ = ("fn", "done")

        def __init__(self, fn, done):
            self.fn = fn
            self.done = done

    def __init__(self, pool: "Pool", batch: int = 500, interval: float = 0.5,
                 checkpoint_interval: float = 5.0):
        self._pool = pool
        self._batch = batch
        self._interval = interval
        self._checkpoint_interval = checkpoint_interval
        self._last_checkpoint = time.monotonic()
        self._q: queue.Queue = queue.Queue()
        # Checksums queued but not yet committed. Reads consult this so a blob
        # that is already hardlinked on disk is never re-downloaded just because
        # its row has not landed yet.
        self._pending: set[str] = set()
        self._pending_lock = threading.Lock()
        self._errors: list[str] = []
        self._idle = threading.Event()
        self._idle.set()
        self._thread = threading.Thread(
            target=self._run, name="mmw-pool-writer", daemon=True)
        self._thread.start()

    def submit(self, row: tuple) -> None:
        with self._pending_lock:
            self._pending.add(row[0])
        self._idle.clear()
        self._q.put(row)

    def pending(self) -> set[str]:
        with self._pending_lock:
            return set(self._pending)

    def submit_task(self, fn, done) -> None:
        """Run `fn()` on the writer thread once pending rows are committed.

        `done(error_or_None)` is called afterwards, on the writer thread.
        """
        self._idle.clear()
        self._q.put(self._Task(fn, done))

    def _run(self) -> None:
        pending: list[tuple] = []
        last = time.monotonic()
        while True:
            try:
                item = self._q.get(timeout=self._interval)
            except queue.Empty:
                item = None
            stopping = item is self._SENTINEL
            task = item if isinstance(item, self._Task) else None
            if item is not None and not stopping and task is None:
                pending.append(item)
            if task is not None and pending:
                self._commit(pending)          # the task must see these rows
                pending = []
                last = time.monotonic()
            if task is not None:
                error = None
                try:
                    task.fn()
                except Exception as e:
                    error = e
                    self._errors.append(str(e))
                try:
                    task.done(error)
                except Exception:
                    pass
            now = time.monotonic()
            if pending and (stopping or len(pending) >= self._batch
                            or now - last >= self._interval):
                self._commit(pending)
                pending = []
                last = now
            if now - self._last_checkpoint >= self._checkpoint_interval:
                self._checkpoint("PASSIVE")
                self._last_checkpoint = now
            if not pending and self._q.empty():
                # Under `_pending_lock`, re-checking the queue: a producer can
                # run `_idle.clear()` + `put()` between the `empty()` test and
                # the `set()`, which would let a `flush()` return with a row
                # still queued. Harmless for today's callers (a reconcile runs
                # *on* this thread, where FIFO order already guarantees its
                # rows are committed) but load-bearing for any future caller
                # that flushes for correctness rather than for freshness.
                with self._pending_lock:
                    if self._q.empty():
                        self._idle.set()
            if stopping:
                # No more writes are coming on this pool: reclaim the WAL fully
                # rather than leave whatever PASSIVE couldn't fold in sitting on
                # disk between sync runs.
                self._checkpoint("TRUNCATE")
                self._idle.set()
                return

    def _commit(self, rows: list[tuple]) -> None:
        try:
            with self._pool._lock:
                self._pool._conn.executemany(
                    "INSERT INTO packages (checksum, checksum_type, filename, size, pool_path) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(checksum) DO UPDATE SET "
                    "checksum_type=excluded.checksum_type, filename=excluded.filename, "
                    "size=excluded.size, pool_path=excluded.pool_path",
                    rows,
                )
                self._pool._conn.commit()
        except Exception as e:                      # never lose the sync to this
            # Rollback explicitly: a failed `executemany` leaves the write
            # transaction OPEN, and sqlite holds it against every other
            # connection (and every other process) until the next successful
            # commit. The rows are lost either way — that is reported through
            # `_errors`, which the sync now reads — but the lock must not be.
            try:
                self._pool._conn.rollback()
            except Exception:
                pass
            self._errors.append(f"{len(rows)} package row(s): {e}")
        finally:
            with self._pending_lock:
                self._pending.difference_update(r[0] for r in rows)

    def _checkpoint(self, mode: str) -> None:
        """Fold the WAL back into the main file. Never lose the sync to this —
        a failed checkpoint just leaves the WAL as it was, still correct, only
        larger than it needs to be. Without this the
        WAL never shrinks while any read or write keeps the pool busy."""
        try:
            with self._pool._lock:
                self._pool._conn.execute(f"PRAGMA wal_checkpoint({mode})")
        except Exception as e:
            self._errors.append(f"wal_checkpoint({mode}): {e}")

    def flush(self, timeout: float = 60.0) -> list[str]:
        """Block until every queued row is committed. Returns any errors.

        Called *from* the writer thread this is a no-op rather than a deadlock:
        a queued task already had its rows committed before it ran, and waiting
        on `_idle` there would wait for a thread that is us.

        Callers that must *see* the rows — `reconcile_refs`, `close` — flush
        first. A queued row is invisible to `contains()`, which would at worst
        cost a redundant download, never corruption: the blob is already
        hardlinked into place before the row is queued.
        """
        if threading.current_thread() is self._thread:
            return list(self._errors)
        self._idle.wait(timeout)
        return list(self._errors)

    def stop(self, timeout: float = 60.0) -> list[str]:
        self._q.put(self._SENTINEL)
        self._thread.join(timeout)
        return list(self._errors)


@dataclass(frozen=True)
class BundleInfo:
    id: int
    network: str
    from_stamp: str | None
    to_stamp: str
    out_path: str
    sent: int
    sent_bytes: int
    removed: int
    made: str


@dataclass(frozen=True)
class PublishedInfo:
    root: str
    network: str
    target: str            # 'live' | snapshot stamp
    made: str


@dataclass(frozen=True)
class SnapshotInfo:
    id: int
    network: str
    stamp: str
    label: str | None
    kind: str
    complete: bool
    settings: str
    files: int


class Pool:
    def __init__(self, mirror_dir: Path, read_only: bool = False,
                 defer_writes: bool | None = None,
                 checkpoint_interval: float = 5.0):
        """read_only: open without creating directories or touching the DB —
        for dry-run / verify paths that must not leave artifacts behind.

        defer_writes: queue package rows to a writer thread instead of
        committing inline (see `_DeferredWriter`). Defaults on for writable
        pools; set `MMW_DEFER_WRITES=0` to fall back to the old inline path
        without editing code.

        checkpoint_interval: how often (seconds) the writer thread folds the
        WAL back into the main file with a PASSIVE checkpoint. Exposed mainly
        for tests; production code should rarely need to override it.
        """
        self.pool_dir = mirror_dir / ".pool"
        self._packages_dir = self.pool_dir / "packages"
        self._lock = threading.Lock()
        db_path = self.pool_dir / "mmw.db"
        if read_only:
            if db_path.exists():
                self._conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
            else:
                # Nothing synced yet — empty in-memory DB answers every query with "no".
                self._conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._conn.executescript(SCHEMA)
        else:
            self._packages_dir.mkdir(parents=True, exist_ok=True)
            self._tmp_dir.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            # Pre-kind databases: CREATE TABLE IF NOT EXISTS won't add the
            # column, so patch it in (instant in SQLite; keeps existing pools usable).
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(refs)")}
            if "kind" not in cols:
                self._conn.execute("ALTER TABLE refs ADD COLUMN kind TEXT NOT NULL DEFAULT 'pkg'")
            if "source" not in cols:
                self._conn.execute("ALTER TABLE refs ADD COLUMN source TEXT NOT NULL DEFAULT ''")
            # After the ALTER, never inside SCHEMA: executescript would run this
            # before the column exists on a pre-source database.
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_refs_source ON refs(source)")
            self._conn.commit()

        # ── the read path ────────────────────────────────────────────────────
        # Reads get their own connection and their own lock, so they never queue
        # behind a write. `reconcile_refs` is one transaction by design (readers
        # must see the old ref set or the new one, never a mix) and was measured
        # at **2.3 s** for a 65k-ref mirror; while it held the single shared
        # lock, every other mirror's `batch_contains` stopped dead. WAL exists
        # precisely to allow concurrent readers alongside one writer.
        #
        # One shared read connection rather than one per thread: 32 connections
        # means 32 separate page caches, which measured *slower* on point
        # lookups. Readers still serialize against each other, but they are
        # milliseconds, not seconds.
        #
        # This is not conditioned on `not read_only`. It used to be — the
        # reasoning was "a read_only pool has no writer to contend with" —
        # but that only justifies skipping the *lock contention* half of this;
        # it says nothing about the *cache tuning* half. A read-only pool
        # (verify, dry-run, publish, bundle) still runs `_PREPARE_WORKERS`-many
        # concurrent reads (`publish_flows._run_per_mirror`) against `self._conn`
        # with no `_tune_reads()` at all — a 2 MB default cache, no mmap — and
        # every one of those concurrent reads serialized on `self._lock` since
        # there was no separate reader to give them. Measured live on an 800 MB
        # pool on a single spinning disk: 100% disk utilization, pure reads,
        # while `_PREPARE_WORKERS=4` bought nothing because the connection
        # behind it was shared and locked exactly like a writer's. A bundle
        # that "locks up" during its pre-flight verify phase is this bug, not
        # a hang — it is really running, just serialized and cold-cached.
        self._writer: "_DeferredWriter | None" = None
        self._rlock = threading.Lock()
        self._rconn: "sqlite3.Connection | None" = None
        if db_path.exists():
            self._rconn = sqlite3.connect(
                f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
            _tune_reads(self._rconn)

        if not read_only:
            if defer_writes is None:
                defer_writes = os.environ.get("MMW_DEFER_WRITES", "1") != "0"
            if defer_writes:
                self._writer = _DeferredWriter(self, checkpoint_interval=checkpoint_interval)
            self._reap_stale_tmp()

        # Read-only opens of a pre-kind DB can't ALTER — degrade gracefully:
        # without the column every legacy ref is a package by definition.
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(refs)")}
        self._kind_filter = "AND r.kind = 'pkg' " if "kind" in cols else ""
        self._has_source = "source" in cols
        self._has_mirrors_table = bool(self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='mirrors'"
        ).fetchone())

    @property
    def _tmp_dir(self) -> Path:
        return self.pool_dir / _TMP_DIR

    def _reap_stale_tmp(self) -> None:
        """Remove crash leftovers from .pool/tmp — but never fresh files that a
        concurrently running sync may still be writing."""
        import time
        cutoff = time.time() - _TMP_MAX_AGE
        try:
            for f in self._tmp_dir.iterdir():
                try:
                    if f.is_file() and f.stat().st_mtime < cutoff:
                        f.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    @contextmanager
    def _read(self):
        """A connection for reading, never blocked by a write.

        Gets the dedicated, tuned reader (`_rconn`) whenever there's a DB file
        to open a second connection on — read-only pools included, so verify/
        dry-run/publish/bundle's concurrent per-mirror reads actually run
        concurrently instead of serializing on the same lock a writer would
        use. Falls back to the shared connection (under `self._lock`) only for
        an in-memory pool (nothing synced yet — there is no file to open a
        second connection on at all).

        NOT for whole-table aggregates — `stats()` and `mirror_stats()` take
        seconds and would hold the read lock for their whole duration, so they
        each open a private short-lived connection instead.
        """
        if self._rconn is None:
            with self._lock:
                yield self._conn
        else:
            with self._rlock:
                yield self._rconn

    def close(self):
        if self._writer is not None:
            self._writer.stop()
            self._writer = None
        if self._rconn is not None:
            self._rconn.close()
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def contains(self, checksum: str) -> bool:
        if self._writer is not None and checksum in self._writer.pending():
            return True
        with self._read() as _c:
            row = _c.execute(
                "SELECT 1 FROM packages WHERE checksum = ?", (checksum,)
            ).fetchone()
        return row is not None


    def batch_contains(self, checksums: list[str]) -> set[str]:
        if not checksums:
            return set()
        # Rows queued for the writer count as present: the blob is on disk, only
        # its row is in flight. Missing this re-downloads what we already have.
        found: set[str] = (self._writer.pending() & set(checksums)
                           if self._writer is not None else set())
        # Lock per chunk, not around the whole scan: a 100k-checksum verify is
        # 100+ queries, and holding the single shared connection for all of them
        # stalled every other pool caller (measured ~150 ms) — sync workers,
        # publish and verify all queue on this one mutex.
        for i in range(0, len(checksums), _CHUNK):
            chunk = checksums[i : i + _CHUNK]
            placeholders = ",".join("?" * len(chunk))
            with self._read() as _c:
                rows = _c.execute(
                    f"SELECT checksum FROM packages WHERE checksum IN ({placeholders})",
                    chunk,
                ).fetchall()
            found.update(row[0] for row in rows)
        return found





    # ── Pool-first API (v2): blobs + desired-state refs ──────────────────────

    def add(
        self,
        source: Path | bytes,
        checksum: str,
        checksum_type: str,
        filename: str,
        size: int,
    ) -> Path:
        """Add content to the pool without linking it anywhere else.

        source is either a tmp file on the pool's filesystem (linked into
        place, then removed) or raw bytes (written via .pool/tmp first).
        The pool inode never has nlink == 1 mid-operation, so a concurrent
        prune can't reap it; an existing entry for the checksum is reused.

        Raises ValueError when `checksum` is not a hex digest or `filename`
        carries a path — see `_validate_blob_name`. Both become path
        components, and a metadata checksum is upstream-supplied.
        """
        _validate_blob_name(checksum, filename)
        pool_dest = self._packages_dir / checksum[:2] / f"{checksum}-{filename}"
        pool_dest.parent.mkdir(parents=True, exist_ok=True)

        if isinstance(source, bytes):
            tmp = self._tmp_dir / f"{os.getpid()}-{threading.get_ident()}-{filename}"
            tmp.write_bytes(source)
        else:
            tmp = source

        try:
            os.link(tmp, pool_dest)
        except FileExistsError:
            pass  # same checksum added concurrently or already present — reuse
        finally:
            tmp.unlink(missing_ok=True)

        row = (checksum, checksum_type, filename, size, str(pool_dest))
        if self._writer is not None:
            # The blob is already hardlinked into place, so the durable fact is
            # on disk before the row is queued. Returning here is what frees the
            # download thread — and its semaphore permit — for the next socket.
            self._writer.submit(row)
            return pool_dest
        with self._lock:
            self._conn.execute(
                "INSERT INTO packages (checksum, checksum_type, filename, size, pool_path) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(checksum) DO UPDATE SET "
                "checksum_type=excluded.checksum_type, filename=excluded.filename, "
                "size=excluded.size, pool_path=excluded.pool_path",
                row,
            )
            self._conn.commit()
        return pool_dest

    def flush_writes(self, timeout: float = 60.0) -> list[str]:
        """Commit anything queued. Returns error descriptions, never raises."""
        return self._writer.flush(timeout) if self._writer is not None else []

    def take_write_errors(self) -> list[str]:
        """Drain and return deferred-writer errors seen since the last call.

        `_DeferredWriter._commit` catches everything so a bad batch cannot kill
        the writer thread and strand the sync — but the resulting list had no
        reader anywhere in the codebase, so ENOSPC, `database is locked` or a
        corrupt DB silently dropped up to 500 package rows per failed batch.
        The blobs stay on disk unreferenced, refs point at packages the
        `refs ⋈ packages` join cannot find, publish ships a short tree, and
        `mmw sync --quiet` exits 0. The sync drains this at the end of every
        mirror and fails the mirror if anything is in it.
        """
        if self._writer is None:
            return []
        errs = list(self._writer._errors)
        del self._writer._errors[:len(errs)]
        return errs

    def submit_reconcile(self, mirror_name: str, entries: list[tuple[str, str, str]],
                         source: str, done) -> bool:
        """Queue a reconcile onto the writer thread. False if there is no writer.

        `reconcile_refs` is one ~2.3 s transaction on a 65k-ref mirror, and it
        used to run via `run_in_executor` — on one of the 32 threads that also
        stream downloads, blocked on the write lock the whole time. Profiled
        after the `add()` fix: **46% of a worker's samples at 1.1% CPU**, i.e. a
        download slot held open doing nothing. The writer thread already owns
        the write lock's rhythm, so the transaction costs no download capacity
        there.
        """
        if self._writer is None:
            return False
        self._writer.submit_task(
            lambda: self.reconcile_refs(mirror_name, entries, source), done)
        return True

    def reconcile_refs(
        self,
        mirror_name: str,
        entries: list[tuple[str, str, str]],
        source: str = "",
    ) -> None:
        """Replace a mirror's refs with exactly `entries` — the desired state.

        entries: (checksum, repo_path, kind). One transaction: concurrent WAL
        readers (publish, verify) see the old set or the new set, never a mix.
        Call ONLY after the sync's metadata committed cleanly; an aborted sync
        must leave the previous consistent snapshot in place.
        """
        # Refs are the desired state and are joined against `packages` at
        # publish time, so every queued row must be committed first.
        self.flush_writes()
        with self._lock:
            self._conn.execute("DELETE FROM refs WHERE mirror_name = ?", (mirror_name,))
            self._conn.executemany(
                "INSERT OR IGNORE INTO refs (checksum, mirror_name, repo_path, kind, source) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (checksum, mirror_name, repo_path, kind, source)
                    for checksum, repo_path, kind in entries
                ],
            )
            self._conn.commit()

    def has_index_refs(self, mirror_name: str) -> bool:
        """Does this mirror currently have any index or release refs?

        `reconcile_refs` replaces a mirror's refs with exactly what it is
        handed, so committing a package-only set would delete the mirror's
        whole index. The sync uses this to tell "first sync, nothing to lose"
        apart from "we already had an index and the new metadata set is
        empty" — only the second is destructive, and only it aborts the flip.
        """
        with self._read() as conn:
            row = conn.execute(
                "SELECT 1 FROM refs WHERE mirror_name = ? AND kind IN (?, ?) LIMIT 1",
                (mirror_name, KIND_META, KIND_RELEASE),
            ).fetchone()
        return row is not None

    def mirror_files(
        self,
        mirror_names: list[str],
        kinds: tuple[str, ...] | None = None,
    ) -> list[tuple[str, str, int, str, str]]:
        """(repo_path, pool_path, size, kind, checksum) for everything the
        mirrors want AND we have.

        Deduplicated across mirrors (component mirrors share suite-level files)
        and ordered by repo_path so consumers get directory locality.
        Desired-but-missing entries (refs without packages) are excluded —
        publish links what exists; verify reports the gap.
        """
        if not mirror_names:
            return []
        m_ph = ",".join("?" * len(mirror_names))
        kind_sql = ""
        params: list = list(mirror_names)
        if kinds:
            kind_sql = f" AND r.kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        with self._read() as _c:
            rows = _c.execute(
                f"SELECT DISTINCT r.repo_path, p.pool_path, p.size, r.kind, r.checksum "
                f"FROM refs r JOIN packages p ON p.checksum = r.checksum "
                f"WHERE r.mirror_name IN ({m_ph}){kind_sql} "
                f"ORDER BY r.repo_path",
                params,
            ).fetchall()
        return rows

    def record_mirror(self, mirror) -> None:
        """Remember what a mirror is, so publish can reconstruct it offline.

        Called as a mirror syncs. A row here without refs means a sync that
        never committed — `mirrors()` joins against refs so those stay invisible.
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO mirrors (name, source, provider, source_url, arch, suite, "
                "component, label, include_sources, dist_upgrade) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "source=excluded.source, provider=excluded.provider, "
                "source_url=excluded.source_url, arch=excluded.arch, "
                "suite=excluded.suite, component=excluded.component, "
                "label=excluded.label, include_sources=excluded.include_sources, "
                "dist_upgrade=excluded.dist_upgrade",
                (
                    mirror.name, getattr(mirror, "source", ""), mirror.provider,
                    mirror.source_url, mirror.arch, mirror.suite, mirror.component,
                    getattr(mirror, "label", ""), int(mirror.include_sources),
                    int(getattr(mirror, "dist_upgrade", False)),
                ),
            )
            self._conn.commit()

    def mirrors(self, sources: list[str] | None = None) -> list:
        """Every mirror the pool actually holds, as MirrorConfig objects.

        This is what publish, certs and network editing enumerate — not
        `cfg.mirrors`, which expands only the architectures the config happens
        to spell out. Restricted to `sources` when given, so a repository
        removed from the config stops being offered.
        """
        from mmw.core.config import MirrorConfig

        if not self._has_mirrors_table:
            return []
        sql = (
            "SELECT m.name, m.source, m.provider, m.source_url, m.arch, m.suite, "
            "m.component, m.label, m.include_sources, m.dist_upgrade FROM mirrors m "
            "WHERE EXISTS (SELECT 1 FROM refs r WHERE r.mirror_name = m.name)"
        )
        params: list = []
        if sources is not None:
            if not sources:
                return []
            sql += f" AND m.source IN ({','.join('?' * len(sources))})"
            params = list(sources)
        sql += " ORDER BY m.name"
        with self._read() as _c:
            rows = _c.execute(sql, params).fetchall()
        return [
            MirrorConfig(
                name=r[0], source=r[1], provider=r[2], source_url=r[3], arch=r[4],
                suite=r[5], component=r[6], label=r[7],
                include_sources=bool(r[8]), dist_upgrade=bool(r[9]),
            )
            for r in rows
        ]

    def list_mirrors(self) -> list[str]:
        """Every mirror the pool holds refs for — what was *actually* synced.

        Publish and network editing enumerate from here rather than from config
        expansion, because architectures are discovered rather than declared.
        """
        with self._read() as _c:
            rows = _c.execute(
                "SELECT DISTINCT mirror_name FROM refs ORDER BY mirror_name"
            ).fetchall()
        return [r[0] for r in rows]

    def list_sources(self) -> list[str]:
        """Repository URIs the pool holds refs for (empty string = pre-source rows)."""
        if not self._has_source:
            return []
        with self._read() as _c:
            rows = _c.execute(
                "SELECT DISTINCT source FROM refs WHERE source != '' ORDER BY source"
            ).fetchall()
        return [r[0] for r in rows]

    def mirrors_for_source(self, source: str) -> list[str]:
        if not self._has_source:
            return []
        with self._read() as _c:
            rows = _c.execute(
                "SELECT DISTINCT mirror_name FROM refs WHERE source = ? ORDER BY mirror_name",
                (source,),
            ).fetchall()
        return [r[0] for r in rows]

    def find_blob(self, repo_path: str) -> Path | None:
        """Pool path for a repo_path referenced by ANY mirror (e.g. the
        changelogs.ubuntu.com meta-release files, synced by whichever Ubuntu
        main mirror ran last)."""
        with self._read() as _c:
            row = _c.execute(
                "SELECT p.pool_path FROM refs r JOIN packages p ON p.checksum = r.checksum "
                "WHERE r.repo_path = ? LIMIT 1",
                (repo_path,),
            ).fetchone()
        return Path(row[0]) if row else None

    def missing_for(self, mirror_name: str) -> list[tuple[str, str]]:
        """(repo_path, checksum) the mirror's index wants but the pool lacks.

        Flushes queued writes first. `_DeferredWriter` batches package rows
        (500 rows / 0.5 s), and `contains`/`batch_contains` paper over that by
        consulting the writer's pending set — this query cannot, because it is
        a SQL anti-join against the `packages` table. Without the flush a blob
        that is already hardlinked into place but whose row is still queued
        reads as missing, so a verify run right after a sync reports a
        perfectly complete mirror as incomplete and the publish gate refuses
        it. Correctness read, not a hot path.
        """
        self.flush_writes()
        with self._read() as _c:
            rows = _c.execute(
                "SELECT r.repo_path, r.checksum FROM refs r "
                "WHERE r.mirror_name = ? "
                "AND NOT EXISTS (SELECT 1 FROM packages p WHERE p.checksum = r.checksum) "
                "ORDER BY r.repo_path",
                (mirror_name,),
            ).fetchall()
        return rows

    def ref_map(self, mirror_name: str) -> dict[str, str]:
        """repo_path → checksum for every ref of a mirror (desired state).

        Path memory for providers whose index carries no checksums, so
        "have we already downloaded this path" is answered by the previous
        sync's refs.
        """
        with self._read() as _c:
            rows = _c.execute(
                "SELECT repo_path, checksum FROM refs WHERE mirror_name = ?",
                (mirror_name,),
            ).fetchall()
        return dict(rows)

    def resolve(self, mirror_name: str, repo_path: str) -> Path | None:
        """Pool path for one (mirror, repo_path) — e.g. to parse a pooled index."""
        with self._read() as _c:
            row = _c.execute(
                "SELECT p.pool_path FROM refs r JOIN packages p ON p.checksum = r.checksum "
                "WHERE r.mirror_name = ? AND r.repo_path = ? LIMIT 1",
                (mirror_name, repo_path),
            ).fetchone()
        return Path(row[0]) if row else None

    def add_copy(self, src: Path, checksum: str, filename: str, size: int) -> Path:
        """`add` a COPY of `src` — `add` moves the file it is given, which is
        right for a download's tmp file and wrong for anything that stays
        where it is (a key, a mirrored upstream file)."""
        self._tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._tmp_dir / f"copy-{os.getpid()}-{threading.get_ident()}-{filename}"
        shutil.copyfile(src, tmp)
        return self.add(tmp, checksum, "sha256", filename, size)

    # ── snapshots ────────────────────────────────────────────────────────────

    def create_snapshot(self, network: str, stamp: str, *, kind: str,
                        rows, label: str | None = None, complete: bool = True,
                        settings: str = "{}") -> int:
        """Record a snapshot and its (path, checksum) rows in one transaction.
        `rows` may be a generator over hundreds of thousands of rows."""
        self.flush_writes()
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO snapshots (network, stamp, label, kind, complete, settings) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (network, stamp, label, kind, int(complete), settings))
            except sqlite3.IntegrityError:
                self._conn.rollback()
                raise ValueError(f"network {network!r} already has a snapshot {stamp}")
            sid = cur.lastrowid
            try:
                # Staged, then resolved to shared ids in three set-wise statements.
                self._conn.execute("CREATE TEMP TABLE IF NOT EXISTS _snap_in "
                                   "(path TEXT PRIMARY KEY, checksum TEXT NOT NULL)")
                self._conn.execute("DELETE FROM _snap_in")
                batch: list[tuple[str, str]] = []
                for path, checksum in rows:
                    batch.append((path, checksum))
                    if len(batch) >= 10_000:
                        self._conn.executemany(
                            "INSERT OR REPLACE INTO _snap_in VALUES (?, ?)", batch)
                        batch.clear()
                if batch:
                    self._conn.executemany("INSERT OR REPLACE INTO _snap_in VALUES (?, ?)", batch)
                self._conn.execute("INSERT OR IGNORE INTO snapshot_paths (path) "
                                   "SELECT path FROM _snap_in")
                self._conn.execute("INSERT OR IGNORE INTO snapshot_blobs (checksum) "
                                   "SELECT checksum FROM _snap_in")
                self._conn.execute(
                    "INSERT INTO snapshot_files (snapshot, path, blob) "
                    "SELECT ?, p.id, b.id FROM _snap_in i "
                    "JOIN snapshot_paths p ON p.path = i.path "
                    "JOIN snapshot_blobs b ON b.checksum = i.checksum", (sid,))
                self._conn.execute("DELETE FROM _snap_in")
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return sid

    def snapshots(self, network: str | None = None) -> list[SnapshotInfo]:
        """Newest first."""
        where, args = ("WHERE s.network = ?", (network,)) if network else ("", ())
        with self._read() as c:
            rows = c.execute(
                "SELECT s.id, s.network, s.stamp, s.label, s.kind, s.complete, s.settings, "
                "(SELECT COUNT(*) FROM snapshot_files f WHERE f.snapshot = s.id) "
                f"FROM snapshots s {where} ORDER BY s.stamp DESC", args).fetchall()
        return [SnapshotInfo(r[0], r[1], r[2], r[3], r[4], bool(r[5]), r[6], r[7])
                for r in rows]

    def snapshot_files(self, snapshot_id: int):
        """(path, checksum) rows, in path order — streamed on a connection of
        its own, so a 700k-row walk never holds the shared reader."""
        self.flush_writes()
        yield from self._stream(
            "SELECT p.path, b.checksum FROM snapshot_files f "
            "JOIN snapshot_paths p ON p.id = f.path JOIN snapshot_blobs b ON b.id = f.blob "
            "WHERE f.snapshot = ? ORDER BY p.path", (snapshot_id,))

    def rename_snapshot(self, snapshot_id: int, label: str | None) -> None:
        with self._lock:
            self._conn.execute("UPDATE snapshots SET label = ? WHERE id = ?",
                               (label or None, snapshot_id))
            self._conn.commit()

    def delete_snapshot(self, snapshot_id: int) -> None:
        """Drop the rows. Its files are freed by the next prune — if nothing
        else (a live mirror, another snapshot) still references them."""
        with self._lock:
            self._conn.execute("DELETE FROM snapshot_files WHERE snapshot = ?", (snapshot_id,))
            self._conn.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))
            # The shared tables hold exactly what some snapshot still lists —
            # prune reads `snapshot_blobs` as the set of held checksums.
            # NOT IN, not a correlated NOT EXISTS: `snapshot_files.path` has no
            # index of its own, and the correlated form compared every path
            # with every row (515k x 515k — it never finished on oak). NOT IN
            # builds its lookup once.
            self._conn.execute("DELETE FROM snapshot_blobs WHERE id NOT IN "
                               "(SELECT blob FROM snapshot_files)")
            self._conn.execute("DELETE FROM snapshot_paths WHERE id NOT IN "
                               "(SELECT path FROM snapshot_files)")
            self._conn.commit()

    def snapshot_blob_rows(self, snapshot_id: int):
        """(path, checksum, pool_path, size) for every file of a snapshot, in
        path order; pool_path is None for a file the pool no longer holds."""
        self.flush_writes()
        yield from self._stream(
            "SELECT p.path, b.checksum, k.pool_path, k.size FROM snapshot_files f "
            "JOIN snapshot_paths p ON p.id = f.path JOIN snapshot_blobs b ON b.id = f.blob "
            "LEFT JOIN packages k ON k.checksum = b.checksum "
            "WHERE f.snapshot = ? ORDER BY p.path", (snapshot_id,))

    def snapshot_missing(self, snapshot_id: int) -> int:
        """How many of a snapshot's files the pool no longer holds."""
        self.flush_writes()
        row = self._read_only(
            "SELECT COUNT(*) FROM snapshot_files f JOIN snapshot_blobs b ON b.id = f.blob "
            "WHERE f.snapshot = ? AND NOT EXISTS "
            "(SELECT 1 FROM packages k WHERE k.checksum = b.checksum)", (snapshot_id,))
        return row[0] if row else 0

    def network_snapshot_paths(self, network: str):
        """Every path any snapshot of `network` has listed — what a full
        bundle reconciles a far side of unknown state against."""
        self.flush_writes()
        yield from (r[0] for r in self._stream(
            "SELECT DISTINCT p.path FROM snapshots s "
            "JOIN snapshot_files f ON f.snapshot = s.id "
            "JOIN snapshot_paths p ON p.id = f.path WHERE s.network = ?", (network,)))

    def record_bundle(self, network: str, from_stamp: str | None, to_stamp: str,
                      out_path: str, sent: int, sent_bytes: int, removed: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO bundles (network, from_stamp, to_stamp, out_path, sent, "
                "sent_bytes, removed) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (network, from_stamp, to_stamp, out_path, sent, sent_bytes, removed))
            self._conn.commit()

    def bundles(self, network: str) -> list[BundleInfo]:
        """Newest first."""
        with self._read() as c:
            rows = c.execute(
                "SELECT id, network, from_stamp, to_stamp, out_path, sent, sent_bytes, "
                "removed, made FROM bundles WHERE network = ? ORDER BY id DESC",
                (network,)).fetchall()
        return [BundleInfo(*r) for r in rows]

    def snapshot_delta(self, from_id: int | None, to_id: int) -> tuple[int, int]:
        """(files, bytes) a bundle from `from_id` (None: full) to `to_id`
        sends: paths whose content differs, and the payload — each distinct
        blob once, as the archive stores a repeat as a hardlink entry."""
        self.flush_writes()
        # (snapshot, path) is the primary key: one indexed probe per row.
        changed = ("" if from_id is None else
                   " AND NOT EXISTS (SELECT 1 FROM snapshot_files a WHERE a.snapshot = ? "
                   "AND a.path = b.path AND a.blob = b.blob)")
        args = (to_id,) if from_id is None else (to_id, from_id)
        row = self._read_only(
            f"WITH sent AS (SELECT b.blob FROM snapshot_files b WHERE b.snapshot = ?{changed}) "
            "SELECT (SELECT COUNT(*) FROM sent), COALESCE(SUM(k.size), 0) "
            "FROM snapshot_blobs sb JOIN packages k ON k.checksum = sb.checksum "
            "WHERE sb.id IN (SELECT blob FROM sent)", args)
        return (row[0], row[1]) if row else (0, 0)

    def record_published(self, root: str, network: str, target: str) -> None:
        """`root` now serves `target` ('live' or a snapshot stamp)."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO published (root, network, target) VALUES (?, ?, ?)",
                (root, network, target))
            self._conn.commit()

    def published(self, network: str) -> list[PublishedInfo]:
        """Every destination this network was published to, newest first."""
        with self._read() as c:
            rows = c.execute(
                "SELECT root, network, target, made FROM published WHERE network = ? "
                "ORDER BY made DESC, root", (network,)).fetchall()
        return [PublishedInfo(*r) for r in rows]

    def snapshot_exclusive_bytes(self, snapshot_ids: "int | list[int]") -> int:
        """Bytes only these snapshots keep — what deleting them (then pruning)
        frees: files no live mirror and no other snapshot references."""
        ids = [snapshot_ids] if isinstance(snapshot_ids, int) else list(snapshot_ids)
        if not ids:
            return 0
        self.flush_writes()
        ph = ",".join("?" * len(ids))
        row = self._read_only(
            "SELECT COALESCE(SUM(p.size), 0) FROM snapshot_blobs b "
            "JOIN packages p ON p.checksum = b.checksum "
            f"WHERE EXISTS (SELECT 1 FROM snapshot_files f WHERE f.blob = b.id "
            f"AND f.snapshot IN ({ph})) "
            f"AND NOT EXISTS (SELECT 1 FROM snapshot_files o WHERE o.blob = b.id "
            f"AND o.snapshot NOT IN ({ph})) "
            "AND NOT EXISTS (SELECT 1 FROM refs r WHERE r.checksum = b.checksum)",
            tuple(ids) * 2)
        return row[0] if row else 0

    def prune_orphans(self, configured_sources: list[str]) -> tuple[int, int]:
        """DB-driven prune: GC refs of unconfigured repositories, then delete
        pool files no remaining ref wants. Returns (files_removed, bytes_freed).

        Keyed on the **repository URI**, never on mirror names. Mirror names
        encode the architecture, and `arches = []` means "every arch upstream
        serves" — so a name-based prune would delete the refs for every arch the
        config does not literally spell out, which after resolution is most of
        them. Asking "is this ref's repository still configured?" is independent
        of how many mirrors that repository happens to expand into.

        Rows written before the `source` column existed carry an empty source
        and are **never** pruned: a destructive operation must be able to prove
        a ref is unwanted, and those rows cannot. The next sync gives them one.

        Files are unlinked BEFORE their rows are deleted: a crash in between
        leaves a stale row (self-healing via the missing-file path), never an
        invisible orphan file. Published network trees hold their own
        hardlinks, so served data survives until republished with prune.

        Raises `PoolBusy` when a sync holds the pool: until a mirror finishes,
        everything it has downloaded is `packages − refs` and would be pruned
        as an orphan.
        """
        with exclusive_pool_lock(self.pool_dir.parent, "prune the pool"):
            return self._prune_orphans_locked(configured_sources)

    def _prune_orphans_locked(self, configured_sources: list[str]) -> tuple[int, int]:
        with self._lock:
            if configured_sources:
                ph = ",".join("?" * len(configured_sources))
                self._conn.execute(
                    f"DELETE FROM refs WHERE source != '' AND source NOT IN ({ph})",
                    configured_sources,
                )
            else:
                self._conn.execute("DELETE FROM refs WHERE source != ''")
            orphans = self._conn.execute(
                "SELECT checksum, pool_path, size FROM packages "
                "WHERE NOT EXISTS (SELECT 1 FROM refs r WHERE r.checksum = packages.checksum) "
                "AND NOT EXISTS (SELECT 1 FROM snapshot_blobs b "
                "WHERE b.checksum = packages.checksum)"
            ).fetchall()
            self._conn.commit()

        removed = 0
        freed = 0
        deleted_checksums: list[str] = []
        for checksum, pool_path, size in orphans:
            try:
                Path(pool_path).unlink(missing_ok=True)
                removed += 1
                freed += size
                deleted_checksums.append(checksum)
            except OSError:
                pass

        if deleted_checksums:
            with self._lock:
                for i in range(0, len(deleted_checksums), _CHUNK):
                    chunk = deleted_checksums[i : i + _CHUNK]
                    ph = ",".join("?" * len(chunk))
                    self._conn.execute(f"DELETE FROM packages WHERE checksum IN ({ph})", chunk)
                self._conn.commit()

        # Clean up empty shard directories
        for d in sorted(self._packages_dir.iterdir(), reverse=True):
            if d.is_dir():
                try:
                    d.rmdir()
                except OSError:
                    pass

        return removed, freed

    def stats(self) -> dict:
        """(packages, total_size) for the whole pool.

        `SUM(size)` cannot use an index, so this is a **full table scan** — on a
        real pool (320k rows, a 435 MB DB with a large WAL overlay, on spinning
        disk) it was measured at 9-14 s. It therefore uses a short-lived
        read-only connection so it never waits on `self._lock`, which 32
        download workers hold constantly: taking that lock made every worker
        block behind the scan and the scan block behind the workers.

        Callers must keep this off any event loop. See `SyncView`.

        Flushes first: this is the figure the sync footer shows, and a count
        that silently lags behind the packages on disk is worse than a
        sub-second wait on a thread that is already off the event loop.
        """
        self.flush_writes()
        row = self._read_only("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM packages")
        return {"packages": row[0], "total_size": row[1]} if row else {
            "packages": 0, "total_size": 0}

    def _stream(self, sql: str, params: tuple = ()):
        """Rows of one query from a short-lived read-only connection, closed
        when the iteration ends (or the generator is dropped)."""
        db_path = self.pool_dir / "mmw.db"
        if not db_path.exists():
            return
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        _tune_reads(conn)
        try:
            yield from conn.execute(sql, params)
        finally:
            conn.close()

    def _read_only(self, sql: str, params: tuple = ()) -> "tuple | None":
        """Run one query on a short-lived read-only connection.

        WAL mode makes this safe alongside a writing sync, and it never queues
        behind `self._lock`. `group_unique_count` established the pattern; the
        expensive aggregates all need it.
        """
        db_path = self.pool_dir / "mmw.db"
        if not db_path.exists():
            return None
        # Read-only URI: a plain connect would CREATE an empty db file if the
        # pool doesn't exist yet.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        _tune_reads(conn)
        try:
            return conn.execute(sql, params).fetchone()
        except sqlite3.OperationalError:
            return None            # schema missing — treat as empty pool
        finally:
            conn.close()

    def group_unique_count(self, mirror_names: list[str]) -> int:
        """Count unique packages referenced by any of the given mirrors."""
        return self.unique_stats(mirror_names)[0]

    def unique_stats(self, mirror_names: list[str]) -> tuple[int, int]:
        """(package_count, total_bytes) of the packages any of the given
        mirrors reference, each counted once — mirrors share files (an
        `Architecture: all` package is in every arch; `-security` packages are
        in `-updates`), so summing `mirror_stats` overcounts badly.

        Uses a short-lived read-only connection so it never waits on self._lock
        (which write workers hold frequently).  WAL mode makes this safe.
        """
        if not mirror_names:
            return 0, 0
        db_path = self.pool_dir / "mmw.db"
        if not db_path.exists():
            return 0, 0
        placeholders = ",".join("?" * len(mirror_names))
        # Read-only URI: a plain connect would CREATE an empty db file if the
        # pool doesn't exist yet (e.g. publish prep before any sync).
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        try:
            row = conn.execute(
                f"SELECT COUNT(*), COALESCE(SUM(p.size), 0) FROM packages p "
                f"WHERE p.checksum IN (SELECT r.checksum FROM refs r "
                f"WHERE r.mirror_name IN ({placeholders}) {self._kind_filter})",
                mirror_names,
            ).fetchone()
        except sqlite3.OperationalError:
            return 0, 0  # schema missing — treat as empty pool
        finally:
            conn.close()
        return (row[0], row[1]) if row else (0, 0)

    def mirror_stats(self, mirror_name: str) -> tuple[int, int]:
        """(package_count, total_bytes) for one mirror — index files excluded.

        Read-only connection for the same reason as `stats()`: measured at
        1.66 s on a real pool, which is far too long to hold `self._lock` or to
        run from a UI callback.
        """
        self.flush_writes()
        row = self._read_only(
            f"SELECT COUNT(*), COALESCE(SUM(p.size), 0) "
            f"FROM packages p JOIN refs r ON p.checksum = r.checksum "
            f"WHERE r.mirror_name = ? {self._kind_filter}",
            (mirror_name,),
        )
        return (row[0], row[1]) if row else (0, 0)



def wipe(mirror_dir: Path) -> None:
    """Delete the entire pool directory.

    Raises `PoolBusy` if a sync is running — deleting the pool underneath one
    is the most destructive version of the prune-during-sync race.
    """
    with exclusive_pool_lock(mirror_dir, "wipe the pool"):
        pool_dir = mirror_dir / ".pool"
        shutil.rmtree(pool_dir, ignore_errors=True)


def verify_checksum(path: Path, expected: str, algo: str) -> bool:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest() == expected
