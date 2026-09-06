import hashlib
import os
import queue
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path


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

# In-flight downloads land here before being linked into packages/.
_TMP_DIR = "tmp"
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

    def __init__(self, pool: "Pool", batch: int = 500, interval: float = 0.5):
        self._pool = pool
        self._batch = batch
        self._interval = interval
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
            if not pending and self._q.empty():
                self._idle.set()
            if stopping:
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
            self._errors.append(f"{len(rows)} package row(s): {e}")
        finally:
            with self._pending_lock:
                self._pending.difference_update(r[0] for r in rows)

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


class Pool:
    def __init__(self, mirror_dir: Path, read_only: bool = False,
                 defer_writes: bool | None = None):
        """read_only: open without creating directories or touching the DB —
        for dry-run / verify paths that must not leave artifacts behind.

        defer_writes: queue package rows to a writer thread instead of
        committing inline (see `_DeferredWriter`). Defaults on for writable
        pools; set `MMW_DEFER_WRITES=0` to fall back to the old inline path
        without editing code.
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
        self._writer: "_DeferredWriter | None" = None
        self._rlock = threading.Lock()
        self._rconn: "sqlite3.Connection | None" = None
        if not read_only and db_path.exists():
            self._rconn = sqlite3.connect(
                f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
            _tune_reads(self._rconn)

        if not read_only:
            if defer_writes is None:
                defer_writes = os.environ.get("MMW_DEFER_WRITES", "1") != "0"
            if defer_writes:
                self._writer = _DeferredWriter(self)
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

        Falls back to the shared connection (under the write lock) when there is
        no separate reader: a `read_only=True` pool has no writer to contend
        with, and an in-memory pool has no file to open a second connection on.

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
        """
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
        """(repo_path, checksum) the mirror's index wants but the pool lacks."""
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
        """
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
                "WHERE NOT EXISTS (SELECT 1 FROM refs r WHERE r.checksum = packages.checksum)"
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

        Callers must keep this off any event loop. See `SyncApp`.

        Flushes first: this is the figure the sync footer shows, and a count
        that silently lags behind the packages on disk is worse than a
        sub-second wait on a thread that is already off the event loop.
        """
        self.flush_writes()
        row = self._read_only("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM packages")
        return {"packages": row[0], "total_size": row[1]} if row else {
            "packages": 0, "total_size": 0}

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
        """Count unique packages referenced by any of the given mirrors.

        Uses a short-lived read-only connection so it never waits on self._lock
        (which write workers hold frequently).  WAL mode makes this safe.
        """
        if not mirror_names:
            return 0
        db_path = self.pool_dir / "mmw.db"
        if not db_path.exists():
            return 0
        placeholders = ",".join("?" * len(mirror_names))
        # Read-only URI: a plain connect would CREATE an empty db file if the
        # pool doesn't exist yet (e.g. publish prep before any sync).
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, check_same_thread=False)
        try:
            row = conn.execute(
                f"SELECT COUNT(DISTINCT r.checksum) FROM refs r "
                f"WHERE r.mirror_name IN ({placeholders}) {self._kind_filter}",
                mirror_names,
            ).fetchone()
        except sqlite3.OperationalError:
            return 0  # schema missing — treat as empty pool
        finally:
            conn.close()
        return row[0] if row else 0

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
    """Delete the entire pool directory."""
    pool_dir = mirror_dir / ".pool"
    shutil.rmtree(pool_dir, ignore_errors=True)


def verify_checksum(path: Path, expected: str, algo: str) -> bool:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest() == expected
