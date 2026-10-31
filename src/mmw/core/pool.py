import fcntl
import heapq
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import islice
from operator import itemgetter
from pathlib import Path

from mmw.core import disk_io
from mmw.core.disk_io.writer import BatchWriter


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


_PRUNE_REPORT_EVERY = 5000   # prune progress cadence, in blobs unlinked

NAME_MAX = 255  # bytes in one path component, on every filesystem a pool lives on


def fit_name(name: str) -> str:
    """`name` cut to NAME_MAX bytes. Pool and tmp names prefix the upstream
    filename with a digest or ids, and a filename that fits on its own can
    overflow then — PyPI has a 211-byte wheel name. Callers keep their unique
    part at the front, so cutting the tail never makes two names collide."""
    if len(name) <= NAME_MAX // 4 or (len(name) <= NAME_MAX and name.isascii()):
        return name
    return name.encode()[:NAME_MAX].decode("utf-8", "ignore")


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

-- Resume identity for downloads without an upstream checksum. Kept separate
-- from publish refs, which only become visible when a unit finishes syncing.
CREATE TABLE IF NOT EXISTS download_sources (
    url           TEXT NOT NULL,
    checksum_type TEXT NOT NULL,
    checksum      TEXT NOT NULL,
    PRIMARY KEY (url, checksum_type)
);

-- Small recovery journal, replaced atomically with each committed VS Code
-- snapshot. An empty journal is distinct from an older, unindexed mirror.
CREATE TABLE IF NOT EXISTS retry_state (
    mirror_name TEXT PRIMARY KEY,
    generation TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS retry_failures (
    mirror_name TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    request TEXT NOT NULL,
    PRIMARY KEY (mirror_name, repo_path)
) WITHOUT ROWID;

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
-- a GC root, like `refs` — prune never deletes a file a complete snapshot
-- still lists. `complete = 0` (history only): imported from a bundle manifest
-- written before snapshots existed, or a transferred snapshot deleted since;
-- its file list is exact but it holds no files, so some may already be
-- pruned — a bundle's starting point, never its destination.
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
    made        TEXT NOT NULL DEFAULT (strftime('%Y%m%dT%H%M%SZ', 'now')),
    kind        TEXT NOT NULL DEFAULT 'bundle',  -- 'bundle' | 'stream' | 'stage'
    runtime     TEXT                             -- JSON: the mmw runtime it carried, if any
);

-- A repository's content at one moment, per network: what the far side
-- reverts. A snapshot is a set of these; an unchanged repository keeps its
-- version (same digest of its rows), so numbers count real changes. Rows
-- outlive the snapshots that held them: the ledger may still name one.
CREATE TABLE IF NOT EXISTS repo_versions (
    id      INTEGER PRIMARY KEY,
    network TEXT NOT NULL,
    repo    TEXT NOT NULL,                    -- repos.repository_key
    number  INTEGER NOT NULL,                 -- v1, v2, … per (network, repo)
    digest  TEXT NOT NULL,
    stamp   TEXT NOT NULL,                    -- the snapshot that introduced it
    label   TEXT,
    UNIQUE (network, repo, digest),
    UNIQUE (network, repo, number)
);
CREATE TABLE IF NOT EXISTS snapshot_repos (
    snapshot INTEGER NOT NULL,
    version  INTEGER NOT NULL,                -- repo_versions.id
    PRIMARY KEY (snapshot, version)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_snapshot_repos_version ON snapshot_repos(version, snapshot);
-- Which version of each repository a bundle (or stream, or stage) sent.
CREATE TABLE IF NOT EXISTS bundle_repos (
    bundle       INTEGER NOT NULL,            -- bundles.id
    repo         TEXT NOT NULL,
    from_version INTEGER,                     -- NULL: sent in full
    to_version   INTEGER NOT NULL,
    PRIMARY KEY (bundle, repo)
) WITHOUT ROWID;

-- What each publish destination serves: 'live' (the network's current state)
-- or a snapshot's stamp. A snapshot some root serves cannot be deleted.
CREATE TABLE IF NOT EXISTS published (
    root     TEXT PRIMARY KEY,                -- absolute directory
    network  TEXT NOT NULL,
    target   TEXT NOT NULL,                   -- 'live' | snapshot stamp
    made     TEXT NOT NULL DEFAULT (strftime('%Y%m%dT%H%M%SZ', 'now'))
);

-- A network whose far side may sit at a snapshot force-deleted here: the
-- record of what it has is gone, so its next default transfer is full.
-- What a network serves instead of the pure upstream rows of one base: its
-- layer's debs and rebuilt indexes (`core/layers.py`). checksum '' = the
-- path is not served (a signature an unsigned layer drops).
CREATE TABLE IF NOT EXISTS layer_rows (
    network   TEXT NOT NULL,
    base      TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    checksum  TEXT NOT NULL,
    size      INTEGER NOT NULL,
    kind      TEXT NOT NULL,
    filename  TEXT NOT NULL,
    PRIMARY KEY (network, base, repo_path)
) WITHOUT ROWID;

-- The inputs a base's layer rows were built from; equal means skip.
CREATE TABLE IF NOT EXISTS layer_digests (
    network TEXT NOT NULL,
    base    TEXT NOT NULL,
    digest  TEXT NOT NULL,
    PRIMARY KEY (network, base)
);

CREATE TABLE IF NOT EXISTS full_next (
    network  TEXT PRIMARY KEY,
    stamp    TEXT NOT NULL                    -- the snapshot forgotten
);

-- mirror_stats / group_unique_count / ref_map all filter on mirror_name;
-- without this they full-scan refs on every detail-panel highlight.

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
    dist_upgrade    INTEGER NOT NULL DEFAULT 0,
    dims            TEXT NOT NULL DEFAULT '{}'
);
-- mirrors.dims: the unit's coordinates as JSON (`MirrorConfig.dims`). The
-- arch/suite/component columns are still written so an older mmw can read
-- this pool, but `dims` is what this version reads. (Kept out of the CREATE
-- TABLE: SQLite cannot rewrite a column list with a comment in it.)
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


class PoolDatabaseMissing(RuntimeError):
    """The configured database is not there, but the pool already holds blobs.

    Starting a fresh, empty database would make every blob an orphan: the next
    prune deletes them and the next sync downloads everything again."""


# `settings.database` for each pool, set by `config.load` and by the TUI when
# it saves settings. Pools are opened from ~40 places that only know
# `mirror_dir`; they ask here rather than each being handed the path.
_DATABASES: dict[Path, Path] = {}


def default_database(mirror_dir: Path) -> Path:
    return Path(mirror_dir) / ".pool" / "mmw.db"


def register_database(mirror_dir: Path, database: Path) -> None:
    _DATABASES[Path(mirror_dir).resolve()] = Path(database)


def database_path(mirror_dir: Path) -> Path:
    """Where this pool's database is: `settings.database`, else the default."""
    return _DATABASES.get(Path(mirror_dir).resolve(), default_database(mirror_dir))


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
            disk_io.ops.mkdir(path.parent, parents=True, exist_ok=True)
            fh = disk_io.ops.open_file(path, "a+")
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


# Pools read in immutable mode (`immutable_reads`): another mmw instance a
# site only reads, perhaps on a network share — no locks, no -shm, and no
# write may happen. Keyed by the resolved mirror directory.
_IMMUTABLE: set[str] = set()


@contextmanager
def immutable_reads(mirror_dir: Path):
    """Within it, every Pool of `mirror_dir` opens read-only and SQLite-
    immutable (`?immutable=1`): no lock files, no shared memory, safe on a
    share — and only right while no other process writes that pool. Opening
    it writable raises."""
    key = str(Path(mirror_dir).resolve())
    _IMMUTABLE.add(key)
    try:
        yield
    finally:
        _IMMUTABLE.discard(key)


def holds_shared_pool_lock(job):
    """Decorate `job(network, cfg, mirror_dir, ...)` to run holding the pool's
    shared lock, as sync and snapshots do: a database Move or a prune
    (exclusive) then waits for a publish or bundle instead of pulling the
    database or blobs out from under it."""
    import functools

    @functools.wraps(job)
    def run(network, cfg, mirror_dir, *args, **kwargs):
        if not disk_io.ops.is_dir(Path(mirror_dir) / ".pool"):
            # No pool, nothing to protect — and taking the lock would create
            # `.pool/` for its lock file as a side effect.
            return job(network, cfg, mirror_dir, *args, **kwargs)
        with shared_pool_lock(mirror_dir):
            return job(network, cfg, mirror_dir, *args, **kwargs)
    return run


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
        disk_io.ops.mkdir(path.parent, parents=True, exist_ok=True)
        fh = disk_io.ops.open_file(path, "a+")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if fh is not None:
            fh.close()
        raise PoolBusy(
            f"refusing to {what}: the pool at {mirror_dir} is in use by a running "
            f"sync, publish or bundle. A prune during a sync deletes the blobs it has just "
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


def _write_download_sources(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    """Write source identities inside the caller's locked blob transaction."""
    conn.executemany(
        "INSERT INTO download_sources (url, checksum_type, checksum) VALUES (?, ?, ?) "
        "ON CONFLICT(url, checksum_type) DO UPDATE SET checksum=excluded.checksum",
        [(row[5], row[1].lower(), row[0]) for row in rows if len(row) > 5 and row[5]],
    )


class _DeferredWriter(BatchWriter):
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

    def __init__(self, pool: "Pool", batch: int = 500, interval: float = 0.5,
                 checkpoint_interval: float = 5.0):
        self._pool = pool
        self._checkpoint_interval = checkpoint_interval
        self._last_checkpoint = time.monotonic()
        # Checksums queued but not yet committed. Reads consult this so a blob
        # that is already hardlinked on disk is never re-downloaded just because
        # its row has not landed yet.
        self._pending: set[str] = set()
        self._pending_sources: dict[tuple[str, str], str] = {}
        self._pending_lock = threading.Lock()
        super().__init__(self._commit_rows, name="mmw-pool-writer", batch=batch,
                         interval=interval)

    def submit(self, row: tuple) -> None:
        with self._pending_lock:
            self._pending.add(row[0])
            if len(row) > 5 and row[5]:
                self._pending_sources[(row[5], row[1].lower())] = row[0]
        super().submit(row)

    def pending(self) -> set[str]:
        with self._pending_lock:
            return set(self._pending)

    def contains_pending(self, checksum: str) -> bool:
        with self._pending_lock:
            return checksum in self._pending

    def pending_sources(self, sources: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
        with self._pending_lock:
            return {key: self._pending_sources[key] for key in sources if key in self._pending_sources}

    def _on_tick(self, now: float) -> None:
        if now - self._last_checkpoint >= self._checkpoint_interval:
            self._checkpoint("PASSIVE")
            self._last_checkpoint = now

    def _on_stop(self) -> None:
        # No more writes are coming on this pool: reclaim the WAL fully rather
        # than leave whatever PASSIVE couldn't fold in sitting on disk between
        # sync runs.
        self._checkpoint("TRUNCATE")

    def _commit_rows(self, rows: list[tuple]) -> None:
        try:
            with self._pool._lock:
                self._pool._conn.executemany(
                    "INSERT INTO packages (checksum, checksum_type, filename, size, pool_path) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(checksum) DO UPDATE SET "
                    "checksum_type=excluded.checksum_type, filename=excluded.filename, "
                    "size=excluded.size, pool_path=excluded.pool_path",
                    [row[:5] for row in rows],
                )
                _write_download_sources(self._pool._conn, rows)
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
                for row in rows:
                    if len(row) > 5 and row[5]:
                        key = (row[5], row[1].lower())
                        if self._pending_sources.get(key) == row[0]:
                            self._pending_sources.pop(key, None)

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
    kind: str = "bundle"


@dataclass(frozen=True)
class RepoVersionInfo:
    id: int
    network: str
    repo: str
    number: int
    digest: str
    stamp: str
    label: str | None


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


def _legacy_dims(arch: str | None, suite: str | None, component: str | None) -> dict[str, str]:
    """`dims` for a mirrors row written before the column existed."""
    dims = {"arch": arch or ""}
    if suite is not None:
        dims["suite"] = suite
    if component is not None:
        dims["component"] = component
    return dims


def _current_dims(raw: str | None, arch: str | None, suite: str | None,
                  component: str | None) -> dict[str, str]:
    """A mirrors row's coordinates. The stored JSON, except that the legacy
    columns win for every key it already has: an older mmw re-recording a
    mirror updates only those columns, so where they disagree they are the
    newer truth. They never add a key — a unit with no arch must not gain
    `arch: ""` from an empty column."""
    legacy = _legacy_dims(arch, suite, component)
    if not raw or raw == "{}":
        return legacy
    dims = json.loads(raw)
    return {k: legacy.get(k, v) for k, v in dims.items()}


def _delete_snapshot_rows(conn: sqlite3.Connection, ids: list[int]) -> None:
    """A snapshot's rows, and the shared paths and checksums no snapshot
    lists any more. The caller holds the write lock and commits."""
    for sid in ids:
        conn.execute("DELETE FROM snapshot_files WHERE snapshot = ?", (sid,))
        conn.execute("DELETE FROM snapshot_repos WHERE snapshot = ?", (sid,))
        conn.execute("DELETE FROM snapshots WHERE id = ?", (sid,))
    # The shared tables hold exactly what some snapshot still lists —
    # prune finds a file's holders through `snapshot_blobs`.
    # NOT IN, not a correlated NOT EXISTS: `snapshot_files.path` has no
    # index of its own, and the correlated form compared every path
    # with every row (515k x 515k — it never finished on oak). NOT IN
    # builds its lookup once.
    conn.execute("DELETE FROM snapshot_blobs WHERE id NOT IN (SELECT blob FROM snapshot_files)")
    conn.execute("DELETE FROM snapshot_paths WHERE id NOT IN (SELECT path FROM snapshot_files)")


def _replace_retry_plan(conn, mirror_name, failures, source):
    """Caller owns the pool write lock and transaction, as for blob batches."""
    conn.execute("DELETE FROM retry_failures WHERE mirror_name = ?", (mirror_name,))
    conn.execute("DELETE FROM retry_state WHERE mirror_name = ?", (mirror_name,))
    if failures is None:
        return None
    token = uuid.uuid4().hex
    conn.execute("INSERT INTO retry_state VALUES (?, ?, ?)", (mirror_name, token, source))
    conn.executemany("INSERT INTO retry_failures VALUES (?, ?, ?)",
                     [(mirror_name, f["package"]["repo_path"], json.dumps(f)) for f in failures])
    return token


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
        immutable = str(Path(mirror_dir).resolve()) in _IMMUTABLE
        if immutable and not read_only:
            raise RuntimeError(f"{mirror_dir} is another instance's pool, read only here")
        ro = "?mode=ro&immutable=1" if immutable else "?mode=ro"
        self.pool_dir = mirror_dir / ".pool"
        self._packages_dir = self.pool_dir / "packages"
        self._lock = threading.Lock()
        db_path = database_path(mirror_dir)
        self.db_path = db_path
        if not disk_io.ops.exists(db_path) and next(disk_io.ops.glob(self._packages_dir, "*/*"), None) is not None:
            raise PoolDatabaseMissing(
                f"the pool database is not at {db_path}, but {self._packages_dir} holds "
                "blobs — move the database there, or point settings.database at it")
        if read_only:
            if disk_io.ops.exists(db_path):
                self._conn = sqlite3.connect(f"file:{db_path}{ro}", uri=True, check_same_thread=False)
            else:
                # Nothing synced yet — empty in-memory DB answers every query with "no".
                self._conn = sqlite3.connect(":memory:", check_same_thread=False)
                self._conn.executescript(SCHEMA)
        else:
            disk_io.ops.mkdir(self._packages_dir, parents=True, exist_ok=True)
            disk_io.ops.mkdir(self._tmp_dir, parents=True, exist_ok=True)
            disk_io.ops.mkdir(db_path.parent, parents=True, exist_ok=True)
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
            # Covering indexes for the listings (`mirror_files`, `missing_for`,
            # snapshot rows): answered from index pages alone, walked in
            # checksum order. A listing that hopped from an index to each
            # table row did one random read per file — most of an hour for a
            # 1M-file mirror on a spinning disk. After the ALTERs for the same
            # reason as idx_refs_source; the cover replaces idx_refs_mirror,
            # a prefix of it. Built once, on the first writable open.
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_refs_cover "
                "ON refs(mirror_name, checksum, repo_path, kind)")
            self._conn.execute("DROP INDEX IF EXISTS idx_refs_mirror")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_packages_cover "
                "ON packages(checksum, size, filename)")
            # Pre-dims databases: add the column, then fill it from the
            # legacy columns once. Rows already carrying dims are untouched.
            bcols = {r[1] for r in self._conn.execute("PRAGMA table_info(bundles)")}
            if "kind" not in bcols:
                self._conn.execute("ALTER TABLE bundles ADD COLUMN kind TEXT NOT NULL DEFAULT 'bundle'")
            if "runtime" not in bcols:
                self._conn.execute("ALTER TABLE bundles ADD COLUMN runtime TEXT")
            mcols = {r[1] for r in self._conn.execute("PRAGMA table_info(mirrors)")}
            if "dims" not in mcols:
                self._conn.execute("ALTER TABLE mirrors ADD COLUMN dims TEXT NOT NULL DEFAULT '{}'")
            # Fill pre-dims rows, and repair rows an older mmw re-recorded
            # (it updates the legacy columns only, leaving dims stale).
            rows = self._conn.execute(
                "SELECT name, dims, arch, suite, component FROM mirrors").fetchall()
            stale = []
            for n, raw, a, s, c in rows:
                fresh = json.dumps(_current_dims(raw, a, s, c), sort_keys=True)
                if fresh != raw:
                    stale.append((fresh, n))
            self._conn.executemany("UPDATE mirrors SET dims = ? WHERE name = ?", stale)
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
        if disk_io.ops.exists(db_path):
            self._rconn = sqlite3.connect(
                f"file:{db_path}{ro}", uri=True, check_same_thread=False)
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
        self._has_mirrors_table = self._has_table("mirrors")
        self._has_mirror_dims = self._has_mirrors_table and "dims" in {
            r[1] for r in self._conn.execute("PRAGMA table_info(mirrors)")}
        # A read-only open of a pool from before snapshots has no such table;
        # checked once here, not per query on a connection a writer may hold.
        self._has_snapshot_blobs = self._has_table("snapshot_blobs")
        self._has_layer_rows = self._has_table("layer_rows")
        self._has_download_sources = self._has_table("download_sources")
        self._has_retry_state = self._has_table("retry_state")
        # SQLite's planner prefers the checksum primary key (one row, but a
        # second hop to the table) over the covering index, so listings name
        # it — only when it exists: a read-only open of an older database
        # has none until a writable open builds it.
        self._packages_by = (" INDEXED BY idx_packages_cover"
                             if self._has_index("idx_packages_cover") else "")

    @property
    def _tmp_dir(self) -> Path:
        return self.pool_dir / _TMP_DIR

    def _reap_stale_tmp(self) -> None:
        """Remove crash leftovers from .pool/tmp — but never fresh files that a
        concurrently running sync may still be writing."""
        disk_io.ops.reap_older_than(self._tmp_dir, time.time() - _TMP_MAX_AGE)

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
        if self._writer is not None and self._writer.contains_pending(checksum):
            return True
        with self._read() as _c:
            row = _c.execute(
                "SELECT 1 FROM packages WHERE checksum = ?", (checksum,)
            ).fetchone()
        return row is not None


    def batch_contains(self, checksums: list[str]) -> set[str]:
        if not checksums:
            return set()
        # A single C-level sort of 200k checksums holds the GIL long enough to
        # freeze the UI even on a worker thread. Sort small runs and merge in
        # Python, retaining global index locality with regular GIL handoffs.
        unique = set(checksums)
        # Rows queued for the writer count as present: the blob is on disk, only
        # its row is in flight. Missing this re-downloads what we already have.
        found: set[str] = (self._writer.pending() & unique
                           if self._writer is not None else set())
        checksums = list(unique)
        del unique
        ordered = heapq.merge(*(sorted(checksums[i:i + _CHUNK]) for i in range(0, len(checksums), _CHUNK)))
        # Lock per chunk, not around the whole scan: a 100k-checksum verify is
        # 100+ queries, and holding the single shared connection for all of them
        # stalled every other pool caller (measured ~150 ms) — sync workers,
        # publish and verify all queue on this one mutex.
        for i in range(0, len(checksums), _CHUNK):
            chunk = list(islice(ordered, _CHUNK))
            placeholders = ",".join("?" * len(chunk))
            with self._read() as _c:
                rows = _c.execute(
                    f"SELECT checksum FROM packages WHERE checksum IN ({placeholders})",
                    chunk,
                ).fetchall()
            found.update(row[0] for row in rows)
        return found





    # ── Pool-first API (v2): blobs + desired-state refs ──────────────────────

    def download_checksums(self, sources: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
        """Batch indexed URL/hash-algorithm lookups, including queued writes.

        A stale mapping is ignored when its package has been pruned. Older
        read-only pools can answer without needing a schema migration.
        """
        sources = list(dict.fromkeys((url, algorithm.lower()) for url, algorithm in sources))
        found = self._writer.pending_sources(sources) if self._writer is not None else {}
        if not sources or not self._has_download_sources:
            return found
        for index in range(0, len(sources), _CHUNK // 2):
            chunk = sources[index:index + _CHUNK // 2]
            placeholders = ','.join('(?,?)' for _ in chunk)
            with self._read() as conn:
                rows = conn.execute(
                    f"WITH requested(url, checksum_type) AS (VALUES {placeholders}) "
                    "SELECT d.url, d.checksum_type, d.checksum FROM requested q "
                    "JOIN download_sources d ON d.url=q.url AND d.checksum_type=q.checksum_type "
                    "JOIN packages p ON p.checksum=d.checksum AND lower(p.checksum_type)=d.checksum_type",
                    [value for key in chunk for value in key],
                ).fetchall()
            for url, algorithm, checksum in rows:
                found.setdefault((url, algorithm), checksum)
        return found

    def add(
        self,
        source: Path | bytes,
        checksum: str,
        checksum_type: str,
        filename: str,
        size: int,
        *,
        source_url: str = "",
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
        pool_dest = self._packages_dir / checksum[:2] / fit_name(f"{checksum}-{filename}")
        disk_io.ops.mkdir(pool_dest.parent, parents=True, exist_ok=True)

        if isinstance(source, bytes):
            tmp = self._tmp_dir / fit_name(f"{os.getpid()}-{threading.get_ident()}-{filename}")
            disk_io.ops.write_bytes(tmp, source)
        else:
            tmp = source

        try:
            # False: the same checksum was added concurrently or is already
            # present — reuse it.
            disk_io.ops.link_into(tmp, pool_dest)
        finally:
            disk_io.ops.unlink(tmp, missing_ok=True)

        row = (checksum, checksum_type, filename, size, str(pool_dest), source_url)
        if self._writer is not None:
            # The blob is already hardlinked into place, so the durable fact is
            # on disk before the row is queued. Returning here is what frees the
            # download thread — and its semaphore permit — for the next socket.
            self._writer.submit(row)
            return pool_dest
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO packages (checksum, checksum_type, filename, size, pool_path) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(checksum) DO UPDATE SET "
                    "checksum_type=excluded.checksum_type, filename=excluded.filename, "
                    "size=excluded.size, pool_path=excluded.pool_path",
                    row[:5],
                )
                _write_download_sources(self._conn, [row])
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return pool_dest

    def flush_writes(self, timeout: float = 60.0) -> list[str]:
        """Commit anything queued. Returns error descriptions, never raises."""
        return self._writer.flush(timeout) if self._writer is not None else []

    def take_write_errors(self) -> list[str]:
        """Drain and return deferred-writer errors seen since the last call.

        `_DeferredWriter._commit_rows` catches everything so a bad batch cannot kill
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
                         source: str, done, retry_failures=None) -> bool:
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
            lambda: self.reconcile_refs(mirror_name, entries, source, retry_failures), done)
        return True

    def reconcile_refs(
        self,
        mirror_name: str,
        entries: list[tuple[str, str, str]],
        source: str = "",
        retry_failures: list[dict] | None = None,
    ) -> None:
        """Make a mirror's refs exactly `entries`, writing only the difference.

        entries: (checksum, repo_path, kind). One transaction: concurrent WAL
        readers (publish, verify) see the old set or the new set, never a mix.
        Call ONLY after the sync's metadata committed cleanly; an aborted sync
        must leave the previous consistent snapshot in place.
        """
        # Refs are the desired state and are joined against `packages` at
        # publish time, so every queued row must be committed first.
        self.flush_writes()
        with self._lock:
            try:
                # Read and mutate the same snapshot, also excluding another
                # process's writer between the comparison and the commit.
                self._conn.execute("BEGIN IMMEDIATE")
                insert_entries = entries
                if not entries:
                    self._conn.execute("DELETE FROM refs WHERE mirror_name = ?", (mirror_name,))
                else:
                    old = {(cs, path): (kind, old_source)
                           for cs, path, kind, old_source in self._conn.execute(
                               "SELECT checksum, repo_path, kind, source FROM refs WHERE mirror_name = ?",
                               (mirror_name,))}
                    if old:
                        desired = {}
                        for checksum, repo_path, kind in entries:
                            # INSERT OR IGNORE historically kept the first duplicate key.
                            desired.setdefault((checksum, repo_path), (kind, source))
                        removed = old.keys() - desired.keys()
                        if len(removed) == len(old):
                            # No retained rows: one delete is cheaper than N
                            # point deletes and still touches only removed refs.
                            self._conn.execute("DELETE FROM refs WHERE mirror_name = ?", (mirror_name,))
                        else:
                            added = desired.keys() - old.keys()
                            changed = [key for key, value in old.items()
                                       if key in desired and value != desired[key]]
                            self._conn.executemany(
                                "DELETE FROM refs WHERE checksum = ? AND mirror_name = ? AND repo_path = ?",
                                ((cs, mirror_name, path) for cs, path in sorted(removed)),
                            )
                            self._conn.executemany(
                                "UPDATE refs SET kind = ?, source = ? "
                                "WHERE checksum = ? AND mirror_name = ? AND repo_path = ?",
                                ((desired[key][0], desired[key][1], key[0], mirror_name, key[1])
                                 for key in sorted(changed)),
                            )
                            insert_entries = [(cs, path, desired[(cs, path)][0]) for cs, path in sorted(added)]
                self._conn.executemany(
                    "INSERT OR IGNORE INTO refs (checksum, mirror_name, repo_path, kind, source) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [(cs, mirror_name, path, kind, source) for cs, path, kind in insert_entries],
                )
                _replace_retry_plan(self._conn, mirror_name, retry_failures, source)
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def retry_plan(self, mirror_name: str):
        """Read only this mirror's failures, including an indexed empty result."""
        if not self._has_retry_state:
            return None
        with self._read() as conn:
            rows = conn.execute(
                "SELECT s.generation, f.request FROM retry_state s "
                "LEFT JOIN retry_failures f ON f.mirror_name = s.mirror_name "
                "WHERE s.mirror_name = ? ORDER BY f.repo_path", (mirror_name,),
            ).fetchall()
        return (rows[0][0], [json.loads(r[1]) for r in rows if r[1] is not None]) if rows else None

    def _check_retry_witness(self, mirror_name, expected_metadata):
        if isinstance(expected_metadata, str):
            row = self._conn.execute(
                "SELECT generation FROM retry_state WHERE mirror_name = ?", (mirror_name,)).fetchone()
            matches = row is not None and row[0] == expected_metadata
        else:
            current = self._conn.execute(
                "SELECT repo_path, checksum FROM refs WHERE mirror_name = ? "
                "AND kind = ? ORDER BY repo_path, checksum",
                (mirror_name, KIND_META),
            ).fetchall()
            matches = current == sorted(expected_metadata.items())
        if not matches:
            raise RuntimeError("mirror metadata changed during repair; retry the repair")

    def seed_retry_plan(self, mirror_name: str, failures: list[dict], source: str,
                        expected_metadata: dict[str, str]) -> str:
        """One-time backfill for existing mirrors, without requiring a resync."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._check_retry_witness(mirror_name, expected_metadata)
                if self._conn.execute("SELECT 1 FROM retry_state WHERE mirror_name = ?",
                                      (mirror_name,)).fetchone():
                    raise RuntimeError("retry list changed during discovery; retry the repair")
                token = _replace_retry_plan(self._conn, mirror_name, failures, source)
                self._conn.commit()
                return token
            except BaseException:
                self._conn.rollback()
                raise

    def repair_refs(self, mirror_name: str, entries: list[tuple[str, str, str]],
                    source: str, expected_metadata: dict[str, str] | str,
                    recovered_paths: list[str] | None = None) -> None:
        """Add recovered VS Code assets without replacing the desired snapshot.

        Refuse stale repairs if another sync changed the metadata while the
        downloads ran. The check and additions share one write transaction.
        """
        self.flush_writes()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._check_retry_witness(mirror_name, expected_metadata)
                if any(kind != KIND_PKG for _, _, kind in entries):
                    raise ValueError("repair may only add package references")
                self._conn.executemany(
                    "INSERT OR IGNORE INTO refs (checksum, mirror_name, repo_path, kind, source) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [(cs, mirror_name, path, kind, source) for cs, path, kind in entries],
                )
                self._conn.executemany(
                    "DELETE FROM retry_failures WHERE mirror_name = ? AND repo_path = ?",
                    [(mirror_name, path) for path in recovered_paths or ()])
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

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

    def blob_path(self, checksum: str, filename: str) -> Path:
        """Where `add` keeps a blob. Listings derive it rather than read the
        stored `pool_path`: that column is not in the covering index, and it
        goes stale when the pool directory moves."""
        return self._packages_dir / checksum[:2] / fit_name(f"{checksum}-{filename}")

    def _blob_strs(self):
        """`str(blob_path(cs, fn))` for a listing's every row, as one f-string:
        two pathlib joins and a str() cost ~8 s across a 1M-file listing, this
        0.3 s. The same string — the root is already a normalised path, and
        `add` refuses a checksum or filename that could add a component."""
        root = str(self._packages_dir)
        return lambda cs, fn: f"{root}/{cs[:2]}/{fit_name(f'{cs}-{fn}')}"

    def _mirror_files_sql(self, mirror_names: list[str], kinds: tuple[str, ...] | None,
                          prefix: str = "") -> tuple[str, list]:
        m_ph = ",".join("?" * len(mirror_names))
        kind_sql = ""
        params: list = list(mirror_names)
        if kinds:
            kind_sql = f" AND r.kind IN ({','.join('?' * len(kinds))})"
            params.extend(kinds)
        if prefix:
            # A range, not LIKE: exact bytes, no escaping, and sqlite filters
            # it on the index entry before the join to packages.
            kind_sql += " AND r.repo_path >= ? AND r.repo_path < ?"
            params.extend((prefix, prefix + "\U0010ffff"))
        # No DISTINCT / ORDER BY: `mirror_files` does both in Python. SQLite
        # did them with a temp B-tree over every row — 9.8 s of a 1M-file
        # listing against 3.7 s — and an index in path order does not save
        # it (7.8 s): with several mirrors it must still merge and dedupe.
        return (f"SELECT r.repo_path, p.filename, p.size, r.kind, r.checksum "
                f"FROM refs r JOIN packages p{self._packages_by} ON p.checksum = r.checksum "
                f"WHERE r.mirror_name IN ({m_ph}){kind_sql}", params)

    def mirror_files(
        self,
        mirror_names: list[str],
        kinds: tuple[str, ...] | None = None,
        prefix: str = "",
    ) -> list[tuple[str, str, int, str, str]]:
        """(repo_path, pool_path, size, kind, checksum) for everything the
        mirrors want AND we have — only repo_paths starting with `prefix`,
        when given.

        Deduplicated across mirrors (component mirrors share suite-level files)
        and ordered by repo_path so consumers get directory locality.
        Desired-but-missing entries (refs without packages) are excluded —
        publish links what exists; verify reports the gap.
        """
        if not mirror_names:
            return []
        with self._read() as _c:
            rows = _c.execute(*self._mirror_files_sql(mirror_names, kinds, prefix)).fetchall()
        if len(mirror_names) > 1:
            # One mirror's rows are already distinct (refs' primary key).
            rows = list(dict.fromkeys(rows))
        rows.sort(key=itemgetter(0))
        blob = self._blob_strs()
        return [(rel, blob(cs, fn), size, kind, cs) for rel, fn, size, kind, cs in rows]

    def record_mirror(self, mirror) -> None:
        """Remember what a mirror is, so publish can reconstruct it offline.

        Called as a mirror syncs. A row here without refs means a sync that
        never committed — `mirrors()` joins against refs so those stay invisible.
        """
        with self._lock:
            self._conn.execute(
                "INSERT INTO mirrors (name, source, provider, source_url, arch, suite, "
                "component, label, include_sources, dist_upgrade, dims) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "source=excluded.source, provider=excluded.provider, "
                "source_url=excluded.source_url, arch=excluded.arch, "
                "suite=excluded.suite, component=excluded.component, "
                "label=excluded.label, include_sources=excluded.include_sources, "
                "dist_upgrade=excluded.dist_upgrade, dims=excluded.dims",
                (
                    mirror.name, mirror.source, mirror.provider, mirror.source_url,
                    mirror.arch, mirror.suite, mirror.component, mirror.label,
                    int(mirror.include_sources), int(mirror.dist_upgrade),
                    json.dumps(mirror.dims, sort_keys=True),
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
            "m.component, m.label, m.include_sources, m.dist_upgrade"
            + (", m.dims" if self._has_mirror_dims else "")
            + " FROM mirrors m "
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
        units = []
        for r in rows:
            dims = _current_dims(r[10] if self._has_mirror_dims else None, r[4], r[5], r[6])
            units.append(MirrorConfig(
                name=r[0], source=r[1], provider=r[2], source_url=r[3], label=r[7],
                dims=dims,
                options={"include_sources": bool(r[8]), "dist_upgrade": bool(r[9])},
            ))
        return units

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
                "SELECT r.checksum, p.filename FROM refs r JOIN packages p ON p.checksum = r.checksum "
                "WHERE r.repo_path = ? LIMIT 1",
                (repo_path,),
            ).fetchone()
        return self.blob_path(*row) if row else None

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
            rows = _c.execute(*self._missing_for_sql(mirror_name)).fetchall()
        return rows

    @staticmethod
    def _missing_for_sql(mirror_name: str) -> tuple[str, tuple]:
        return ("SELECT r.repo_path, r.checksum FROM refs r "
                "WHERE r.mirror_name = ? "
                "AND NOT EXISTS (SELECT 1 FROM packages p WHERE p.checksum = r.checksum) "
                "ORDER BY r.repo_path", (mirror_name,))

    def meta_refs(self, mirror_name: str) -> dict[str, str]:
        """repo_path → checksum for a mirror's `meta` refs: the saved metadata a
        retry is checked against (`seed_retry_plan`, `repair_refs`)."""
        with self._read() as _c:
            rows = _c.execute(
                "SELECT repo_path, checksum FROM refs WHERE mirror_name = ? AND kind = ?",
                (mirror_name, KIND_META),
            ).fetchall()
        return dict(rows)

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
                "SELECT r.checksum, p.filename FROM refs r JOIN packages p ON p.checksum = r.checksum "
                "WHERE r.mirror_name = ? AND r.repo_path = ? LIMIT 1",
                (mirror_name, repo_path),
            ).fetchone()
        return self.blob_path(*row) if row else None

    def add_copy(self, src: Path, checksum: str, filename: str, size: int) -> Path:
        """`add` a COPY of `src` — `add` moves the file it is given, which is
        right for a download's tmp file and wrong for anything that stays
        where it is (a key, a mirrored upstream file)."""
        disk_io.ops.mkdir(self._tmp_dir, parents=True, exist_ok=True)
        tmp = self._tmp_dir / fit_name(f"copy-{os.getpid()}-{threading.get_ident()}-{filename}")
        disk_io.ops.copy_file(src, tmp)
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
        self._delete_snapshots([snapshot_id])

    def demote_snapshot(self, snapshot_id: int) -> None:
        """Make it history only: its rows stay — a far side may sit at it, and
        a transfer starts from its list and versions — but it stops holding
        its files, so the next prune frees what nothing else references."""
        with self._lock:
            self._conn.execute("UPDATE snapshots SET complete = 0 WHERE id = ?", (snapshot_id,))
            self._conn.commit()

    def _delete_snapshots(self, ids: list[int]) -> None:
        with self._lock:
            _delete_snapshot_rows(self._conn, ids)
            self._conn.commit()

    _LAST_SENT = ("SELECT r.repo, r.to_version FROM bundle_repos r JOIN bundles b ON b.id = r.bundle "
                  "WHERE b.network = ? ORDER BY b.id")

    def forget_snapshot(self, snap: SnapshotInfo, far_side_at_it: bool) -> bool:
        """Force delete: the snapshot's rows and every transfer that went to
        it, in one transaction. Version rows stay — a far side may hold one,
        and a number must never be handed out again. When the far side may be
        at it (`far_side_at_it`: where the next transfer would start), or a
        repository's last-sent version came from one of those transfers, the
        network's next default transfer is full. Returns whether it now is."""
        network, stamp = snap.network, snap.stamp
        with self._lock:
            before = dict(self._conn.execute(self._LAST_SENT, (network,)).fetchall())
            went = "SELECT id FROM bundles WHERE network = ? AND to_stamp = ?"
            self._conn.execute(f"DELETE FROM bundle_repos WHERE bundle IN ({went})", (network, stamp))
            self._conn.execute("DELETE FROM bundles WHERE network = ? AND to_stamp = ?", (network, stamp))
            full = far_side_at_it or dict(
                self._conn.execute(self._LAST_SENT, (network,)).fetchall()) != before
            if full:
                self._conn.execute("INSERT OR REPLACE INTO full_next (network, stamp) VALUES (?, ?)",
                                   (network, stamp))
            _delete_snapshot_rows(self._conn, [snap.id])
            self._conn.commit()
        return full

    def full_next(self, network: str) -> str | None:
        """The forgotten snapshot that makes `network`'s next default
        transfer full, if one does."""
        try:
            with self._read() as c:
                row = c.execute("SELECT stamp FROM full_next WHERE network = ?", (network,)).fetchone()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):        # read-only open of an older pool
                return None
            raise
        return row[0] if row else None

    def clear_full_next(self, network: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM full_next WHERE network = ?", (network,))
            self._conn.commit()

    def network_names(self) -> set[str]:
        """Every network with snapshots, bundles or publish records here."""
        with self._read() as c:
            return {r[0] for r in c.execute(
                "SELECT network FROM snapshots UNION SELECT network FROM bundles "
                "UNION SELECT network FROM published")}

    def rename_network(self, old: str, new: str) -> str | None:
        """File a network's snapshots, bundle log and publish records under its
        new name. Returns a problem line instead when `new` already has some
        (merging two histories would make "where did the last bundle go"
        meaningless), else None."""
        if old == new:
            return None
        with self._lock:
            clash = self._conn.execute(
                "SELECT 1 FROM snapshots WHERE network = ? UNION ALL "
                "SELECT 1 FROM bundles WHERE network = ? LIMIT 1", (new, new)).fetchone()
            if clash:
                return (f"snapshots or bundles of a network called '{new}' are already in "
                        f"the pool — '{old}''s were left under the old name")
            for table in ("snapshots", "bundles", "published", "repo_versions", "full_next",
                          "layer_rows", "layer_digests"):
                self._conn.execute(f"UPDATE {table} SET network = ? WHERE network = ?",
                                   (new, old))
            self._conn.commit()
        return None

    def forget_network(self, network: str) -> int:
        """Drop a removed network's snapshots, bundle log and publish records.
        Returns how many snapshots went; the next prune frees their files.

        Left behind, they were GC roots nothing could reach — every snapshot
        command names a configured network — and a network added later under
        the same name inherited the old one's bundle history."""
        ids = [s.id for s in self.snapshots(network)]
        if ids:
            self._delete_snapshots(ids)
        with self._lock:
            self._conn.execute("DELETE FROM bundle_repos WHERE bundle IN "
                               "(SELECT id FROM bundles WHERE network = ?)", (network,))
            self._conn.execute("DELETE FROM repo_versions WHERE network = ?", (network,))
            self._conn.execute("DELETE FROM bundles WHERE network = ?", (network,))
            self._conn.execute("DELETE FROM published WHERE network = ?", (network,))
            self._conn.execute("DELETE FROM full_next WHERE network = ?", (network,))
            self._conn.execute("DELETE FROM layer_rows WHERE network = ?", (network,))
            self._conn.execute("DELETE FROM layer_digests WHERE network = ?", (network,))
            self._conn.commit()
        return len(ids)

    # ── per-network layers ──────────────────────────────────────────────────

    def replace_layer(self, network: str, base: str, rows, digest: str) -> None:
        """`rows`: (repo_path, checksum, size, kind, filename); checksum '' =
        not served. Replaces the base's rows and digest in one transaction."""
        with self._lock:
            self._conn.execute("DELETE FROM layer_rows WHERE network = ? AND base = ?", (network, base))
            self._conn.executemany(
                "INSERT INTO layer_rows VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(network, base, *row) for row in rows])
            self._conn.execute("INSERT OR REPLACE INTO layer_digests VALUES (?, ?, ?)",
                               (network, base, digest))
            self._conn.commit()

    def clear_layer(self, network: str, base: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM layer_rows WHERE network = ? AND base = ?", (network, base))
            self._conn.execute("DELETE FROM layer_digests WHERE network = ? AND base = ?", (network, base))
            self._conn.commit()

    def layer_overlay(self, network: str, base: str) -> dict:
        """repo_path → a `mirror_files`-shaped row, or None (not served)."""
        try:
            with self._read() as c:
                rows = c.execute("SELECT repo_path, checksum, size, kind, filename FROM layer_rows "
                                 "WHERE network = ? AND base = ?", (network, base)).fetchall()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return {}
            raise
        blob = self._blob_strs()
        return {rel: ((rel, blob(cs, fn), size, kind, cs) if cs else None)
                for rel, cs, size, kind, fn in rows}

    def layer_bases(self, network: str) -> set[str]:
        try:
            with self._read() as c:
                return {b for (b,) in c.execute(
                    "SELECT base FROM layer_digests WHERE network = ?", (network,))}
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return set()
            raise

    def layer_digest(self, network: str, base: str) -> str | None:
        try:
            with self._read() as c:
                row = c.execute("SELECT digest FROM layer_digests WHERE network = ? AND base = ?",
                                (network, base)).fetchone()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return None
            raise
        return row[0] if row else None

    def snapshot_blob_rows(self, snapshot_id: int):
        """(path, checksum, pool_path, size) for every file of a snapshot, in
        path order; pool_path is None for a file the pool no longer holds."""
        self.flush_writes()
        blob = self._blob_strs()
        for path, cs, fn, size in self._stream(
                "SELECT p.path, b.checksum, k.filename, k.size FROM snapshot_files f "
                "JOIN snapshot_paths p ON p.id = f.path JOIN snapshot_blobs b ON b.id = f.blob "
                f"LEFT JOIN packages k{self._packages_by} ON k.checksum = b.checksum "
                "WHERE f.snapshot = ? ORDER BY p.path", (snapshot_id,)):
            yield path, cs, (blob(cs, fn) if fn is not None else None), size

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
                      out_path: str, sent: int, sent_bytes: int, removed: int, *,
                      kind: str = "bundle",
                      repos: "dict[str, tuple[int | None, int]] | None" = None,
                      runtime: dict | None = None) -> int:
        """Log one completed transfer and, per repository, which version it
        took the far side from and to (ids; from None: in full). `runtime`:
        the mmw runtime it carried ({mmw, python, arches}), if any."""
        with self._lock:
            try:
                cur = self._conn.execute(
                    "INSERT INTO bundles (network, from_stamp, to_stamp, out_path, sent, "
                    "sent_bytes, removed, kind, runtime) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (network, from_stamp, to_stamp, out_path, sent, sent_bytes, removed, kind,
                     json.dumps(runtime, sort_keys=True) if runtime is not None else None))
                bid = cur.lastrowid
                self._conn.executemany(
                    "INSERT INTO bundle_repos (bundle, repo, from_version, to_version) VALUES (?, ?, ?, ?)",
                    [(bid, repo, a, b) for repo, (a, b) in (repos or {}).items()])
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return bid

    def mark_installer_sent(self, network: str, sent: dict) -> None:
        """The newest bundle of `network` went with mmw's installer (`sent`: {mmw, …})."""
        with self._lock:
            c = self._conn
            if "runtime" not in {r[1] for r in c.execute("PRAGMA table_info(bundles)")}:
                return
            c.execute("UPDATE bundles SET runtime = ? WHERE id = (SELECT max(id) FROM bundles WHERE network = ?)",
                      (json.dumps(sent, sort_keys=True), network))
            c.commit()

    def runtime_sent(self, network: str) -> dict | None:
        """The mmw runtime the newest bundle that carried one carried."""
        with self._read() as c:
            if "runtime" not in {r[1] for r in c.execute("PRAGMA table_info(bundles)")}:
                return None
            row = c.execute("SELECT runtime FROM bundles WHERE network = ? AND runtime IS NOT NULL "
                            "ORDER BY id DESC LIMIT 1", (network,)).fetchone()
        return json.loads(row[0]) if row else None

    def bundles(self, network: str) -> list[BundleInfo]:
        """Newest first."""
        with self._read() as c:
            # A read-only open of a pool no newer mmw has opened yet has no
            # `kind` column; every row it holds is a bundle.
            cols = {r[1] for r in c.execute("PRAGMA table_info(bundles)")}
            kind = "kind" if "kind" in cols else "'bundle'"
            rows = c.execute(
                "SELECT id, network, from_stamp, to_stamp, out_path, sent, sent_bytes, "
                f"removed, made, {kind} FROM bundles WHERE network = ? ORDER BY id DESC",
                (network,)).fetchall()
        return [BundleInfo(*r) for r in rows]

    # ── repository versions ──────────────────────────────────────────────────

    _VERSION_COLS = "v.id, v.network, v.repo, v.number, v.digest, v.stamp, v.label"

    def _versions(self, sql: str, args: tuple) -> list[RepoVersionInfo]:
        try:
            with self._read() as c:
                return [RepoVersionInfo(*r) for r in c.execute(sql, args).fetchall()]
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):        # read-only open of an older pool
                return []
            raise

    def record_repo_versions(self, snapshot_id: int, network: str, stamp: str,
                             digests: dict[str, str]) -> dict[str, RepoVersionInfo]:
        """Attach each repository's version to a snapshot: the existing one
        with the same digest, or a new one numbered after the last."""
        self.flush_writes()
        with self._lock:
            try:
                ids = {}
                for repo, digest in sorted(digests.items()):
                    row = self._conn.execute(
                        "SELECT id FROM repo_versions WHERE network = ? AND repo = ? AND digest = ?",
                        (network, repo, digest)).fetchone()
                    if row is None:
                        (top,) = self._conn.execute(
                            "SELECT COALESCE(MAX(number), 0) FROM repo_versions "
                            "WHERE network = ? AND repo = ?", (network, repo)).fetchone()
                        row = (self._conn.execute(
                            "INSERT INTO repo_versions (network, repo, number, digest, stamp) "
                            "VALUES (?, ?, ?, ?, ?)", (network, repo, top + 1, digest, stamp)).lastrowid,)
                    self._conn.execute("INSERT OR IGNORE INTO snapshot_repos (snapshot, version) VALUES (?, ?)",
                                       (snapshot_id, row[0]))
                    ids[repo] = row[0]
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise
        return self.snapshot_repo_versions(snapshot_id)

    def snapshot_repo_versions(self, snapshot_id: int) -> dict[str, RepoVersionInfo]:
        return {v.repo: v for v in self._versions(
            f"SELECT {self._VERSION_COLS} FROM snapshot_repos s JOIN repo_versions v ON v.id = s.version "
            "WHERE s.snapshot = ?", (snapshot_id,))}

    def repo_versions(self, network: str, repo: str | None = None) -> list[RepoVersionInfo]:
        """Newest number first."""
        where, args = ("AND v.repo = ?", (network, repo)) if repo is not None else ("", (network,))
        return self._versions(
            f"SELECT {self._VERSION_COLS} FROM repo_versions v WHERE v.network = ? {where} "
            "ORDER BY v.repo, v.number DESC", args)

    def repo_version(self, version_id: int) -> RepoVersionInfo:
        found = self._versions(f"SELECT {self._VERSION_COLS} FROM repo_versions v WHERE v.id = ?",
                               (version_id,))
        if not found:
            raise LookupError(f"no repository version {version_id}")
        return found[0]

    def repo_version_snapshots(self, version_id: int) -> list[int]:
        """Ids of the snapshots holding a version, newest stamp first."""
        try:
            with self._read() as c:
                return [r[0] for r in c.execute(
                    "SELECT s.id FROM snapshot_repos r JOIN snapshots s ON s.id = r.snapshot "
                    "WHERE r.version = ? ORDER BY s.stamp DESC", (version_id,)).fetchall()]
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return []
            raise

    def last_sent_versions(self, network: str, without: str | None = None) -> dict[str, int]:
        """Repository → the version id the newest transfer naming it sent;
        `without`: as if no transfer had gone to that stamp."""
        sql, args = self._LAST_SENT, (network,)
        if without is not None:
            sql, args = sql.replace("ORDER BY", "AND b.to_stamp != ? ORDER BY"), (network, without)
        try:
            with self._read() as c:
                rows = c.execute(sql, args).fetchall()
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return {}
            raise
        return dict(rows)                       # later bundles overwrite earlier ones

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
        frees: files no live mirror and no other complete snapshot references.
        A history-only snapshot keeps none."""
        ids = [snapshot_ids] if isinstance(snapshot_ids, int) else list(snapshot_ids)
        if not ids:
            return 0
        self.flush_writes()
        ph = ",".join("?" * len(ids))
        row = self._read_only(
            "SELECT COALESCE(SUM(p.size), 0) FROM snapshot_blobs b "
            "JOIN packages p ON p.checksum = b.checksum "
            f"WHERE EXISTS (SELECT 1 FROM snapshot_files f JOIN snapshots s ON s.id = f.snapshot "
            f"WHERE f.blob = b.id AND f.snapshot IN ({ph}) AND s.complete = 1) "
            f"AND NOT EXISTS (SELECT 1 FROM snapshot_files o JOIN snapshots s ON s.id = o.snapshot "
            f"WHERE o.blob = b.id AND o.snapshot NOT IN ({ph}) AND s.complete = 1) "
            "AND NOT EXISTS (SELECT 1 FROM refs r WHERE r.checksum = b.checksum)",
            tuple(ids) * 2)
        return row[0] if row else 0

    def prune_orphans(self, configured_sources: list[str],
                      dropped_mirrors: "list[str] | tuple[str, ...]" = (), *,
                      stop_event=None, progress=None) -> tuple[int, int]:
        """DB-driven prune: GC refs of unconfigured repositories and of
        `dropped_mirrors`, then delete pool files no remaining ref and no
        snapshot wants. Returns (files_removed, bytes_freed).

        Keyed on the **repository URI**, never on mirror names. Mirror names
        encode the architecture, and `arches = []` means "every arch upstream
        serves" — so a name-based prune would delete the refs for every arch the
        config does not literally spell out, which after resolution is most of
        them. Asking "is this ref's repository still configured?" is independent
        of how many mirrors that repository happens to expand into.

        `dropped_mirrors` is the one exception, and it is computed from the
        config by the caller (`resolve.uncovered_mirror_names`): mirrors of a
        repository that IS still configured but whose suite, component or arch
        the config no longer covers — narrowing `suites` would otherwise leave
        the dropped suite's refs, and its files, in the pool forever.

        Rows written before the `source` column existed carry an empty source
        and are **never** pruned: a destructive operation must be able to prove
        a ref is unwanted, and those rows cannot. The next sync gives them one.
        An empty `configured_sources` proves nothing either — it would make
        every ref unwanted — so it is refused outright (`ValueError`).

        Files are unlinked BEFORE their rows are deleted: a crash in between
        leaves a stale row (self-healing via the missing-file path), never an
        invisible orphan file. Published network trees hold their own
        hardlinks, so served data survives until republished with prune.

        Raises `PoolBusy` when a sync holds the pool: until a mirror finishes,
        everything it has downloaded is `packages − refs` and would be pruned
        as an orphan.
        """
        if not configured_sources:
            raise ValueError(
                "refusing to prune against no configured repositories: every ref in "
                "the pool would look unwanted and prune would empty the whole pool")
        with exclusive_pool_lock(self.pool_dir.parent, "prune the pool"):
            return self._prune_orphans_locked(configured_sources, list(dropped_mirrors),
                                              stop_event=stop_event, progress=progress)

    def _orphans_sql(self, ref_filter: str = "1",
                     columns: str = "checksum, pool_path, size") -> str:
        """Packages no ref passing `ref_filter` wants and no snapshot holds —
        the one definition of an orphan, shared by the prune and its preview."""
        # Only a complete snapshot holds: a history-only one's files may go.
        # Through idx_snapshot_files_blob, a few index steps per package.
        held = ("AND NOT EXISTS (SELECT 1 FROM snapshot_blobs b "
                "JOIN snapshot_files f ON f.blob = b.id JOIN snapshots s ON s.id = f.snapshot "
                "WHERE b.checksum = packages.checksum AND s.complete = 1)"
                if self._has_snapshot_blobs else "")
        # A network's layer (debs, rebuilt indexes, signatures) is referenced
        # only by layer_rows: without this a prune deleted every live one.
        layered = ("AND NOT EXISTS (SELECT 1 FROM layer_rows l WHERE l.checksum = packages.checksum)"
                   if self._has_layer_rows else "")
        return (f"SELECT {columns} FROM packages "
                "WHERE NOT EXISTS (SELECT 1 FROM refs r WHERE r.checksum = packages.checksum "
                f"AND {ref_filter}) {held} {layered}")

    def _has_table(self, name: str) -> bool:
        """Init-time schema probe — not for use once the pool is shared."""
        return bool(self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())

    def _has_index(self, name: str) -> bool:
        """Init-time schema probe, like `_has_table`."""
        return bool(self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name=?", (name,)).fetchone())

    def prune_preview(self, configured_sources: list[str],
                      dropped_mirrors: "list[str] | tuple[str, ...]" = ()) -> tuple[int, int]:
        """(files, bytes) `prune_orphans` would remove — without writing.

        The same orphan query, with the refs the prune would delete filtered
        out instead of deleted. Snapshot-held blobs are excluded exactly as
        the prune excludes them: the preview used to be a second copy of the
        SQL, and it drifted the moment snapshots became GC roots.
        """
        if self._has_source:
            keep = "(r.source = ''"
            params: list = []
            if configured_sources:
                keep += f" OR r.source IN ({','.join('?' * len(configured_sources))})"
                params += configured_sources
            keep += ")"
            if dropped_mirrors:
                keep += f" AND r.mirror_name NOT IN ({','.join('?' * len(dropped_mirrors))})"
                params += list(dropped_mirrors)
        else:
            # A pre-`source` pool can prove nothing about any ref.
            keep, params = "1", []
        with self._read() as c:
            row = c.execute(self._orphans_sql(keep, "COUNT(*), COALESCE(SUM(size), 0)"),
                            params).fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    def _prune_orphans_locked(self, configured_sources: list[str],
                              dropped_mirrors: list[str], *, stop_event=None,
                              progress=None) -> tuple[int, int]:
        with self._lock:
            ph = ",".join("?" * len(configured_sources))
            self._conn.execute(
                f"DELETE FROM refs WHERE source != '' AND source NOT IN ({ph})",
                configured_sources,
            )
            for i in range(0, len(dropped_mirrors), _CHUNK):
                chunk = dropped_mirrors[i:i + _CHUNK]
                mph = ",".join("?" * len(chunk))
                self._conn.execute(f"DELETE FROM refs WHERE mirror_name IN ({mph})", chunk)
                self._conn.execute(f"DELETE FROM mirrors WHERE name IN ({mph})", chunk)
            self._conn.execute(
                "DELETE FROM retry_failures WHERE mirror_name IN (SELECT s.mirror_name "
                "FROM retry_state s WHERE NOT EXISTS "
                "(SELECT 1 FROM refs r WHERE r.mirror_name = s.mirror_name))")
            self._conn.execute(
                "DELETE FROM retry_state WHERE NOT EXISTS "
                "(SELECT 1 FROM refs r WHERE r.mirror_name = retry_state.mirror_name)")
            orphans = self._conn.execute(self._orphans_sql()).fetchall()
            self._conn.commit()

        removed = 0
        freed = 0
        deleted_checksums: list[str] = []
        # Unlinks on the job's write lane; a stop leaves the rest — files and
        # rows both — for the next prune. A blob already gone counts as
        # removed: its row is what is left to drop.
        with disk_io.DiskJob("prune", stop_event=stop_event) as job:
            for (checksum, _pool_path, size), result in job.meta_map(
                    lambda row: disk_io.ops.remove(row[1]), orphans):
                if isinstance(result, Exception):
                    continue
                removed += 1
                freed += size
                deleted_checksums.append(checksum)
                if progress and removed % _PRUNE_REPORT_EVERY == 0:
                    progress(removed, len(orphans))

        if deleted_checksums:
            with self._lock:
                for i in range(0, len(deleted_checksums), _CHUNK):
                    chunk = deleted_checksums[i : i + _CHUNK]
                    ph = ",".join("?" * len(chunk))
                    self._conn.execute(f"DELETE FROM packages WHERE checksum IN ({ph})", chunk)
                self._conn.commit()

        # Clean up empty shard directories
        for d in sorted(disk_io.ops.iterdir(self._packages_dir), reverse=True):
            if disk_io.ops.is_dir(d):
                try:
                    disk_io.ops.rmdir(d)
                except OSError:
                    pass

        return removed, freed

    def forget_blobs(self, checksums) -> tuple[int, int]:
        """Drop damaged blobs — file and `packages` row — so the next sync
        downloads them again. Returns (blobs, bytes on record).

        A blob whose bytes no longer match its checksum (truncated, missing,
        rewritten) is otherwise permanent: every sync asks "is this checksum
        pooled?", the row says yes, and the bad file is published and bundled
        forever. Refs are left alone — they are what the next sync fills.
        Takes the pool exclusively, like prune: a sync must not be linking
        the same checksum while its row is deleted.
        """
        checksums = sorted(set(checksums))
        if not checksums:
            return 0, 0
        with exclusive_pool_lock(self.pool_dir.parent, "repair the pool"):
            self.flush_writes()
            rows: list[tuple[str, str, int]] = []
            with self._lock:
                for i in range(0, len(checksums), _CHUNK):
                    chunk = checksums[i:i + _CHUNK]
                    rows += self._conn.execute(
                        f"SELECT checksum, pool_path, size FROM packages "
                        f"WHERE checksum IN ({','.join('?' * len(chunk))})", chunk).fetchall()
            with disk_io.DiskJob("repair") as job:
                for _path, result in job.remove(Path(r[1]) for r in rows):
                    if isinstance(result, Exception):
                        raise result        # a blob that will not go keeps its row
            with self._lock:
                for i in range(0, len(rows), _CHUNK):
                    chunk = [r[0] for r in rows[i:i + _CHUNK]]
                    affected = self._conn.execute(
                        f"SELECT DISTINCT mirror_name FROM refs WHERE checksum IN "
                        f"({','.join('?' * len(chunk))})", chunk).fetchall()
                    self._conn.executemany("DELETE FROM retry_failures WHERE mirror_name = ?", affected)
                    self._conn.executemany("DELETE FROM retry_state WHERE mirror_name = ?", affected)
                    self._conn.execute(
                        f"DELETE FROM packages WHERE checksum IN ({','.join('?' * len(chunk))})",
                        chunk)
                self._conn.commit()
        return len(rows), sum(r[2] for r in rows)

    def stats(self) -> dict:
        """(packages, total_size) for the whole pool.

        Answered from the covering `idx_packages_size` index (see SCHEMA);
        before it, a full table scan measured at 9-14 s on a real pool. It
        still uses a short-lived read-only connection so it never waits on
        `self._lock`, which 32 download workers hold constantly: taking that
        lock made every worker block behind the scan and the scan block
        behind the workers.

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
        db_path = self.db_path
        if not disk_io.ops.exists(db_path):
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
        db_path = self.db_path
        if not disk_io.ops.exists(db_path):
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
        """How many package files a publish of these mirrors places: one per
        path the pool holds a blob for. Not one per checksum — VS Code serves
        the same bytes at many paths (an icon across versions), and counting
        checksums ran a publish's progress to 105 %."""
        if not mirror_names:
            return 0
        placeholders = ",".join("?" * len(mirror_names))
        # Distinct paths, counted within each checksum: one COUNT(DISTINCT)
        # over a million paths sorts them all (6-22 s on a 1M-file VS Code
        # mirror); per checksum the groups are tiny and come in index order
        # (~2 s). Exact all the same — a path shared by two mirrors (apt's
        # `Architecture: all`) holds one checksum, so it is in one group — two
        # only across an upstream republish mid-sync, a file or two over.
        row = self._read_only(
            f"SELECT COALESCE(SUM(n), 0) FROM (SELECT COUNT(DISTINCT r.repo_path) n "
            f"FROM refs r WHERE r.mirror_name IN ({placeholders}) {self._kind_filter}"
            f"AND EXISTS (SELECT 1 FROM packages p WHERE p.checksum = r.checksum) "
            f"GROUP BY r.checksum)",
            tuple(mirror_names))
        return row[0] if row else 0

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
        db_path = self.db_path
        if not disk_io.ops.exists(db_path):
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
        db = database_path(mirror_dir)
        disk_io.ops.rmtree(pool_dir, ignore_errors=True)
        # A database placed elsewhere (settings.database) goes with its pool:
        # left behind, it would describe blobs that no longer exist.
        for suffix in ("", "-wal", "-shm"):
            disk_io.ops.unlink(Path(f"{db}{suffix}"), missing_ok=True)


def verify_checksum(path: Path, expected: str, algo: str) -> bool:
    return disk_io.hash_file(path, algo) == expected
