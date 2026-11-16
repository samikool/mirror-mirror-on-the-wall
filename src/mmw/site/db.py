"""The site database: every repository version this site was sent, what
each repository serves, the network-level files, and any switch left
halfway. One file in the site's home.

The home lives on a share, often NFS: no WAL (it needs shared memory a
network filesystem cannot give), and exclusion is the site lock's job, not
SQLite's. Paths and checksums are stored once and shared, as the pool's
snapshot tables do — consecutive versions are nearly identical.
"""
import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

DB_FILE = "site.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS paths (id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS blobs (id INTEGER PRIMARY KEY, checksum TEXT NOT NULL UNIQUE,
                                  size INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY, repo TEXT NOT NULL, number INTEGER NOT NULL, label TEXT NOT NULL,
    digest TEXT NOT NULL, stamp TEXT NOT NULL, snap_label TEXT,
    UNIQUE (repo, number));
CREATE TABLE IF NOT EXISTS version_files (
    version INTEGER NOT NULL, path INTEGER NOT NULL, blob INTEGER NOT NULL,
    PRIMARY KEY (version, path)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_version_files_blob ON version_files(blob, version);
CREATE TABLE IF NOT EXISTS repos (
    repo TEXT PRIMARY KEY, label TEXT NOT NULL, bases TEXT NOT NULL DEFAULT '[]',
    serving INTEGER, held INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS network_files (path TEXT PRIMARY KEY, blob INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_network_files_blob ON network_files(blob);
CREATE TABLE IF NOT EXISTS switches (
    repo TEXT PRIMARY KEY, from_version INTEGER, to_version INTEGER NOT NULL,
    phase TEXT NOT NULL DEFAULT 'pkg');
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deleted (repo TEXT NOT NULL, number INTEGER NOT NULL, stamp TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS site_layers (
    repo TEXT PRIMARY KEY, signing TEXT NOT NULL DEFAULT 'upstream', key TEXT,
    contents INTEGER NOT NULL DEFAULT 0, drop_valid_until INTEGER NOT NULL DEFAULT 0);
-- What a site layer changes in one version of a repository: path → blob
-- ('' = not served). Keyed by the layer's inputs digest.
CREATE TABLE IF NOT EXISTS overlays (
    repo TEXT NOT NULL, version INTEGER NOT NULL, digest TEXT NOT NULL, path TEXT NOT NULL,
    checksum TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (repo, version, digest, path)) WITHOUT ROWID;
-- Timelines: a track is one upstream Release stream of a repository (a
-- suite's dists/ dir, a flat base) or "" for files no Release claims; a
-- point is one Release of it (ident: its digest), dated by its Date:. A
-- stub (held = 0) keeps its file list, not its files. A version is a
-- state: one chosen point per track, listed in versions.points.
CREATE TABLE IF NOT EXISTS tracks (
    id INTEGER PRIMARY KEY, repo TEXT NOT NULL, name TEXT NOT NULL,
    origin TEXT, label TEXT, suite TEXT, codename TEXT,
    pinned INTEGER, previous INTEGER,
    UNIQUE (repo, name));
CREATE TABLE IF NOT EXISTS points (
    id INTEGER PRIMARY KEY, track INTEGER NOT NULL, ident TEXT NOT NULL, date TEXT NOT NULL,
    stamp TEXT NOT NULL, snap_label TEXT, held INTEGER NOT NULL DEFAULT 1,
    UNIQUE (track, ident));
CREATE TABLE IF NOT EXISTS point_files (
    point INTEGER NOT NULL, path INTEGER NOT NULL, blob INTEGER NOT NULL,
    PRIMARY KEY (point, path)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_point_files_blob ON point_files(blob, point);
-- What this far side has sent each other network (its export ledger):
-- written once a bundle is complete, so the next one sends only newer points.
CREATE TABLE IF NOT EXISTS exports (
    target TEXT NOT NULL, repo TEXT NOT NULL, ident TEXT NOT NULL, stamp TEXT NOT NULL,
    files TEXT NOT NULL DEFAULT '',            -- the digest of the files it went with
    PRIMARY KEY (target, repo, ident)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS applied (
    id INTEGER PRIMARY KEY, created TEXT NOT NULL, previous TEXT, kind TEXT NOT NULL,
    applied_at TEXT NOT NULL);
"""


@dataclass(frozen=True)
class VersionInfo:
    id: int
    repo: str
    number: int
    label: str
    digest: str
    stamp: str
    snap_label: str | None


@dataclass(frozen=True)
class TrackInfo:
    id: int
    repo: str
    name: str                 # the Release's dir, or "" for files no Release claims
    suite: str | None
    codename: str | None
    pinned: int | None        # a point id
    previous: int | None      # the point served before the current one
    gone: bool = False        # not in this network's latest bundle: no longer served unless pinned
    origin: str | None = None
    label: str | None = None
    keep: str | None = None   # this suite's own history setting (else the site's)


@dataclass(frozen=True)
class PointInfo:
    id: int
    track: int
    ident: str
    date: str                 # YYYYMMDDTHHMMSSZ, the Release's Date: (else arrival)
    stamp: str                # the bundle that first brought it
    snap_label: str | None
    held: bool                # False: a stub — its file list, not its files


@dataclass(frozen=True)
class SiteLayer:
    repo: str
    signing: str              # upstream | resign | unsigned
    key: str | None
    contents: bool
    drop_valid_until: bool


@dataclass(frozen=True)
class RepoState:
    repo: str
    label: str
    bases: list[str]
    serving: VersionInfo | None
    held: bool
    root: str | None = None      # the root it is placed in; None: the primary


class SiteDB:
    def __init__(self, home: Path):
        self.home = Path(home)                   # site layers live beside the database
        self._conn = sqlite3.connect(Path(home) / DB_FILE)
        self._conn.execute("PRAGMA journal_mode=DELETE")
        self._conn.execute("PRAGMA foreign_keys=OFF")
        self._conn.executescript(SCHEMA)
        repo_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(repos)")}
        if "root" not in repo_cols:
            self._conn.execute("ALTER TABLE repos ADD COLUMN root TEXT")
        if "overlay" not in repo_cols:          # the site layer digest it serves, if any
            self._conn.execute("ALTER TABLE repos ADD COLUMN overlay TEXT")
        switch_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(switches)")}
        if "overlay" not in switch_cols:
            self._conn.execute("ALTER TABLE switches ADD COLUMN overlay TEXT")
        if "held" not in switch_cols:           # held afterwards, as begun (NULL: from before this was kept)
            self._conn.execute("ALTER TABLE switches ADD COLUMN held INTEGER")
        if "points" not in {r[1] for r in self._conn.execute("PRAGMA table_info(versions)")}:
            self._conn.execute("ALTER TABLE versions ADD COLUMN points TEXT")
        if "gone" not in {r[1] for r in self._conn.execute("PRAGMA table_info(tracks)")}:
            self._conn.execute("ALTER TABLE tracks ADD COLUMN gone INTEGER NOT NULL DEFAULT 0")
        if "keep" not in {r[1] for r in self._conn.execute("PRAGMA table_info(tracks)")}:
            self._conn.execute("ALTER TABLE tracks ADD COLUMN keep TEXT")
        # Completeness, once known: every file of the point is in the store
        # (NULL: not checked since its files or the store last changed).
        point_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(points)")}
        if "complete" not in point_cols:
            self._conn.execute("ALTER TABLE points ADD COLUMN complete INTEGER")
        # The slice a point covers ({"arches", "components"} as JSON), worked
        # out once — and again when its files grow (a widened or refilled
        # point). NULL: not yet.
        if "selection" not in point_cols:
            self._conn.execute("ALTER TABLE points ADD COLUMN selection TEXT")

    def __enter__(self) -> "SiteDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # ── interning ────────────────────────────────────────────────────────────

    def _blob_id(self, checksum: str, size: int) -> int:
        c = self._conn
        # A size learnt later (the blob was missing when first listed) fills in the 0.
        c.execute("INSERT INTO blobs (checksum, size) VALUES (?, ?) ON CONFLICT(checksum) DO UPDATE "
                  "SET size = excluded.size WHERE blobs.size = 0 AND excluded.size > 0", (checksum, size))
        return c.execute("SELECT id FROM blobs WHERE checksum = ?", (checksum,)).fetchone()[0]

    def _path_id(self, path: str) -> int:
        c = self._conn
        c.execute("INSERT OR IGNORE INTO paths (path) VALUES (?)", (path,))
        return c.execute("SELECT id FROM paths WHERE path = ?", (path,)).fetchone()[0]

    # ── versions ─────────────────────────────────────────────────────────────

    _V = "id, repo, number, label, digest, stamp, snap_label"

    def add_version(self, repo: str, label: str, number: int, digest: str, stamp: str,
                    snap_label: str | None, bases: list[str],
                    rows: Iterable[tuple[str, str, int]], *, commit: bool = True) -> int:
        """Record a version and its (path, checksum, size) rows. The same
        repository and number with the same digest is the version already
        here; with a different one it is refused."""
        found = self.version(repo, number)
        if found is not None:
            if found.digest != digest:
                raise ValueError(f"{label} as of {found.stamp[:4]}-{found.stamp[4:6]}-{found.stamp[6:8]} here "
                                 "differs from the one in this bundle")
            return found.id
        c = self._conn
        try:
            vid = c.execute("INSERT INTO versions (repo, number, label, digest, stamp, snap_label) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (repo, number, label, digest, stamp, snap_label)).lastrowid
            c.executemany("INSERT INTO version_files (version, path, blob) VALUES (?, ?, ?)",
                          [(vid, self._path_id(p), self._blob_id(cs, size)) for p, cs, size in rows])
            c.execute("INSERT INTO repos (repo, label, bases) VALUES (?, ?, ?) ON CONFLICT(repo) "
                      "DO UPDATE SET label = excluded.label, bases = excluded.bases",
                      (repo, label, json.dumps(bases)))
            if commit:
                c.commit()
        except BaseException:
            c.rollback()
            raise
        return vid

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def _version_rows(self, where: str, args: tuple) -> list[VersionInfo]:
        return [VersionInfo(*r) for r in self._conn.execute(
            f"SELECT {self._V} FROM versions {where}", args).fetchall()]

    def version(self, repo: str, number: int) -> VersionInfo | None:
        found = self._version_rows("WHERE repo = ? AND number = ?", (repo, number))
        return found[0] if found else None

    def version_by_id(self, version_id: int) -> VersionInfo | None:
        found = self._version_rows("WHERE id = ?", (version_id,))
        return found[0] if found else None

    def versions(self, repo: str) -> list[VersionInfo]:
        """Newest number first."""
        return self._version_rows("WHERE repo = ? ORDER BY number DESC", (repo,))

    def version_files(self, version_id: int) -> dict[str, str]:
        return dict(self._conn.execute(
            "SELECT p.path, b.checksum FROM version_files f JOIN paths p ON p.id = f.path "
            "JOIN blobs b ON b.id = f.blob WHERE f.version = ?", (version_id,)).fetchall())

    def delete_version(self, version_id: int) -> None:
        c = self._conn
        # Remembered, so "as of a bundle" can tell a deleted version from one never here.
        c.execute("INSERT INTO deleted (repo, number, stamp) SELECT repo, number, stamp FROM versions "
                  "WHERE id = ?", (version_id,))
        c.execute("DELETE FROM version_files WHERE version = ?", (version_id,))
        c.execute("DELETE FROM overlays WHERE version = ?", (version_id,))
        c.execute("DELETE FROM versions WHERE id = ?", (version_id,))
        c.commit()

    def drop_repo(self, repo: str) -> None:
        """Forget a repository here, every version of it (it moved to another root)."""
        c = self._conn
        for (vid,) in c.execute("SELECT id FROM versions WHERE repo = ?", (repo,)).fetchall():
            c.execute("DELETE FROM version_files WHERE version = ?", (vid,))
        c.execute("DELETE FROM versions WHERE repo = ?", (repo,))
        c.execute("DELETE FROM repos WHERE repo = ?", (repo,))
        c.execute("DELETE FROM point_files WHERE point IN (SELECT p.id FROM points p JOIN tracks t "
                  "ON t.id = p.track WHERE t.repo = ?)", (repo,))
        c.execute("DELETE FROM points WHERE track IN (SELECT id FROM tracks WHERE repo = ?)", (repo,))
        c.execute("DELETE FROM tracks WHERE repo = ?", (repo,))
        c.execute("DELETE FROM overlays WHERE repo = ?", (repo,))        # its site layer went with it
        c.execute("DELETE FROM site_layers WHERE repo = ?", (repo,))
        c.execute("DELETE FROM meta WHERE key = ?", (f"hold:{repo}",))
        c.commit()

    # ── tracks and points ────────────────────────────────────────────────────

    _T = "id, repo, name, suite, codename, pinned, previous, gone, origin, label, keep"
    _P = "id, track, ident, date, stamp, snap_label, held"

    def rename_repo(self, old: str, new: str, label: str, bases: list[str]) -> None:
        """File repository `old` (its timeline, versions, layer, state) under
        `new` — a repository adopted before its network named it."""
        import json
        c = self._conn
        try:
            c.execute("UPDATE repos SET repo = ?, label = ?, bases = ? WHERE repo = ?",
                      (new, label, json.dumps(bases), old))
            for table in ("versions", "tracks", "switches", "deleted", "site_layers", "overlays", "exports"):
                c.execute(f"UPDATE {table} SET repo = ? WHERE repo = ?", (new, old))
            c.execute("UPDATE versions SET label = ? WHERE repo = ?", (label, new))
            for prefix in ("hold:", "moving_in:", "moved:", "local-repo:"):
                if c.execute("SELECT 1 FROM meta WHERE key = ?", (prefix + old,)).fetchone():
                    c.execute("DELETE FROM meta WHERE key = ?", (prefix + new,))      # a stale one gives way
                    c.execute("UPDATE meta SET key = ? WHERE key = ?", (prefix + new, prefix + old))
            foreign = json.loads(self.get_meta("foreign_mirrors") or "{}")
            if old in foreign:
                foreign[new] = foreign.pop(old)
                c.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('foreign_mirrors', ?)",
                          (json.dumps(foreign, sort_keys=True),))
            c.commit()
        except BaseException:
            c.rollback()
            raise

    def ensure_track(self, repo: str, name: str, fields: dict[str, str], *, commit: bool = True) -> int:
        c = self._conn
        c.execute("INSERT INTO tracks (repo, name, origin, label, suite, codename) VALUES (?, ?, ?, ?, ?, ?) "
                  "ON CONFLICT(repo, name) DO UPDATE SET origin = coalesce(excluded.origin, origin), "
                  "label = coalesce(excluded.label, label), suite = coalesce(excluded.suite, suite), "
                  "codename = coalesce(excluded.codename, codename)",
                  (repo, name, fields.get("Origin"), fields.get("Label"), fields.get("Suite"),
                   fields.get("Codename")))
        tid = c.execute("SELECT id FROM tracks WHERE repo = ? AND name = ?", (repo, name)).fetchone()[0]
        if commit:
            c.commit()
        return tid

    def tracks(self, repo: str) -> list[TrackInfo]:
        return [TrackInfo(*r[:7], bool(r[7]), r[8], r[9], r[10]) for r in self._conn.execute(
            f"SELECT {self._T} FROM tracks WHERE repo = ? ORDER BY name", (repo,)).fetchall()]

    def track(self, track_id: int) -> TrackInfo | None:
        row = self._conn.execute(f"SELECT {self._T} FROM tracks WHERE id = ?", (track_id,)).fetchone()
        return TrackInfo(*row[:7], bool(row[7]), row[8], row[9], row[10]) if row else None

    def set_present(self, repo: str, names: set[str], *, commit: bool = True) -> None:
        """The tracks a complete list of the repository has; the rest are gone."""
        for tid, name in self._conn.execute("SELECT id, name FROM tracks WHERE repo = ?", (repo,)).fetchall():
            self._conn.execute("UPDATE tracks SET gone = ? WHERE id = ?", (int(name not in names), tid))
        if commit:
            self._conn.commit()

    def known_complete(self, point_id: int) -> bool:
        row = self._conn.execute("SELECT complete FROM points WHERE id = ?", (point_id,)).fetchone()
        return bool(row and row[0])

    def set_complete(self, point_id: int | None, complete: bool | None) -> None:
        """Remember a point complete (None: forget — `point_id` None: every point)."""
        value = None if complete is None else int(complete)
        if point_id is None:
            self._conn.execute("UPDATE points SET complete = ?", (value,))
        else:
            self._conn.execute("UPDATE points SET complete = ? WHERE id = ?", (value, point_id))
        self._conn.commit()

    def _point(self, row) -> PointInfo:
        return PointInfo(*row[:6], bool(row[6]))

    def import_point(self, track_id: int, draft, sizes: dict[str, int], stamp: str,
                     snap_label: str | None, *, commit: bool = True) -> tuple[int, str]:
        """Add `draft` (a `mmw.site.tracks.PointDraft`) to the track: "new";
        "same" when that Release is here with these files; "widened" when it
        gained files (another selection of the same Release); "refilled" for
        a stub brought back; "conflict:<path>" — refused, nothing changed —
        when a path it already has comes with another checksum."""
        c = self._conn
        row = c.execute(f"SELECT {self._P} FROM points WHERE track = ? AND ident = ?",
                        (track_id, draft.ident)).fetchone()
        try:
            if row is None:
                pid = c.execute("INSERT INTO points (track, ident, date, stamp, snap_label) VALUES (?, ?, ?, ?, ?)",
                                (track_id, draft.ident, draft.date, stamp, snap_label)).lastrowid
                self._add_point_files(pid, draft.files, sizes)
                status = "new"
            else:
                point = self._point(row)
                pid, have = point.id, self.point_files(point.id)
                clash = next((p for p, cs in sorted(draft.files.items()) if p in have and have[p] != cs), None)
                if clash is not None:
                    return pid, f"conflict:{clash}"
                extra = {p: cs for p, cs in draft.files.items() if p not in have}
                self._add_point_files(pid, extra, sizes)
                if extra or not point.held:          # its files grew: worked out again
                    c.execute("UPDATE points SET complete = NULL, selection = NULL WHERE id = ?", (pid,))
                if draft.ident.startswith("content:") and stamp > point.stamp:
                    # Files no Release dates: the same content arriving again
                    # is that content now — it moves to the front of its track.
                    c.execute("UPDATE points SET date = ?, stamp = ? WHERE id = ?", (draft.date, stamp, pid))
                if not point.held:
                    c.execute("UPDATE points SET held = 1 WHERE id = ?", (pid,))
                    status = "refilled"
                else:
                    status = "widened" if extra else "same"
            if commit:
                c.commit()
        except BaseException:
            c.rollback()
            raise
        return pid, status

    def _add_point_files(self, pid: int, files: dict[str, str], sizes: dict[str, int]) -> None:
        self._conn.executemany("INSERT INTO point_files (point, path, blob) VALUES (?, ?, ?)",
                               [(pid, self._path_id(p), self._blob_id(cs, sizes.get(cs, 0)))
                                for p, cs in files.items()])

    def points(self, track_id: int, *, stubs: bool = False) -> list[PointInfo]:
        """Newest date first; one date's points (upstream re-published with
        the same Date) in the order they arrived, then by ident."""
        return [self._point(r) for r in self._conn.execute(
            f"SELECT {self._P} FROM points WHERE track = ? {'' if stubs else 'AND held = 1'} "
            "ORDER BY date DESC, stamp DESC, id DESC", (track_id,)).fetchall()]   # then the later arrival

    def point(self, point_id: int) -> PointInfo | None:
        row = self._conn.execute(f"SELECT {self._P} FROM points WHERE id = ?", (point_id,)).fetchone()
        return self._point(row) if row else None

    def point_files(self, point_id: int) -> dict[str, str]:
        return dict(self._conn.execute(
            "SELECT p.path, b.checksum FROM point_files f JOIN paths p ON p.id = f.path "
            "JOIN blobs b ON b.id = f.blob WHERE f.point = ?", (point_id,)).fetchall())

    def stub_point(self, point_id: int) -> None:
        self._conn.execute("UPDATE points SET held = 0 WHERE id = ?", (point_id,))
        self._conn.commit()

    def set_track_keep(self, track_id: int, keep: str | None) -> None:
        self._conn.execute("UPDATE tracks SET keep = ? WHERE id = ?", (keep, track_id))
        self._conn.commit()

    def set_pin(self, track_id: int, point_id: int | None) -> None:
        self._conn.execute("UPDATE tracks SET pinned = ? WHERE id = ?", (point_id, track_id))
        self._conn.commit()

    def set_previous(self, track_id: int, point_id: int | None, *, commit: bool = True) -> None:
        self._conn.execute("UPDATE tracks SET previous = ? WHERE id = ?", (point_id, track_id))
        if commit:
            self._conn.commit()

    def point_selection(self, point_id: int) -> dict | None:
        row = self._conn.execute("SELECT selection FROM points WHERE id = ?", (point_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def set_point_selection(self, point_id: int, selection: dict) -> None:
        self._conn.execute("UPDATE points SET selection = ? WHERE id = ?", (json.dumps(selection), point_id))
        self._conn.commit()

    def point_sizes(self, point_id: int) -> tuple[int, int]:
        """(bytes its files take, bytes only it holds — what stubbing it frees
        once nothing served needs them)."""
        total = self._conn.execute(
            "SELECT COALESCE(SUM(b.size), 0) FROM blobs b WHERE b.id IN "
            "(SELECT blob FROM point_files WHERE point = ?)", (point_id,)).fetchone()[0]
        # Per blob of this point, an index lookup each — never a list of every
        # other point's, state's or network file (history asks this per point).
        only = self._conn.execute(
            "SELECT COALESCE(SUM(b.size), 0) FROM blobs b WHERE b.id IN "
            "(SELECT blob FROM point_files WHERE point = ?) "
            "AND NOT EXISTS (SELECT 1 FROM point_files f JOIN points p ON p.id = f.point "
            "                WHERE f.blob = b.id AND f.point != ? AND p.held = 1) "
            "AND NOT EXISTS (SELECT 1 FROM version_files v WHERE v.blob = b.id) "
            "AND NOT EXISTS (SELECT 1 FROM network_files n WHERE n.blob = b.id)",
            (point_id, point_id)).fetchone()[0]
        return total, only

    # ── states: one chosen point per track ───────────────────────────────────

    def state(self, repo: str, point_ids: list[int]) -> VersionInfo | None:
        found = self._version_rows("WHERE repo = ? AND points = ?", (repo, json.dumps(sorted(point_ids))))
        return found[0] if found else None

    def add_state(self, repo: str, label: str, bases: list[str], point_ids: list[int], digest: str,
                  stamp: str, snap_label: str | None, rows: Iterable[tuple[str, str, int]], *,
                  commit: bool = True) -> int:
        """A state and its materialised files; the existing one when these
        points already make one."""
        found = self.state(repo, point_ids)
        if found is not None:
            return found.id
        c = self._conn
        try:
            number = c.execute("SELECT COALESCE(MAX(number), 0) + 1 FROM versions WHERE repo = ?",
                               (repo,)).fetchone()[0]
            vid = c.execute("INSERT INTO versions (repo, number, label, digest, stamp, snap_label, points) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (repo, number, label, digest, stamp, snap_label,
                             json.dumps(sorted(point_ids)))).lastrowid
            c.executemany("INSERT INTO version_files (version, path, blob) VALUES (?, ?, ?)",
                          [(vid, self._path_id(p), self._blob_id(cs, size)) for p, cs, size in rows])
            c.execute("INSERT INTO repos (repo, label, bases) VALUES (?, ?, ?) ON CONFLICT(repo) "
                      "DO UPDATE SET label = excluded.label, bases = excluded.bases",
                      (repo, label, json.dumps(bases)))
            if commit:
                c.commit()
        except BaseException:
            c.rollback()
            raise
        return vid

    def ensure_repo(self, repo: str, label: str, bases: list[str], *, commit: bool = True) -> None:
        self._conn.execute("INSERT INTO repos (repo, label, bases) VALUES (?, ?, ?) ON CONFLICT(repo) "
                           "DO UPDATE SET label = excluded.label, bases = excluded.bases",
                           (repo, label, json.dumps(bases)))
        if commit:
            self._conn.commit()

    def blob_sizes(self, checksums: Iterable[str]) -> dict[str, int]:
        wanted = sorted(set(checksums))
        out: dict[str, int] = {}
        for i in range(0, len(wanted), 500):
            chunk = wanted[i:i + 500]
            out.update(self._conn.execute(
                f"SELECT checksum, size FROM blobs WHERE checksum IN ({','.join('?' * len(chunk))})", chunk))
        return out

    def state_points(self, version_id: int) -> list[int]:
        row = self._conn.execute("SELECT points FROM versions WHERE id = ?", (version_id,)).fetchone()
        return json.loads(row[0]) if row and row[0] else []

    def set_state_points(self, version_id: int, point_ids: list[int], *, commit: bool = True) -> None:
        self._conn.execute("UPDATE versions SET points = ? WHERE id = ?",
                           (json.dumps(sorted(point_ids)), version_id))
        if commit:
            self._conn.commit()

    def drop_unused_states(self, repo: str) -> int:
        """Forget the states nothing serves or is switching between — their
        files are kept by their points. How many went."""
        c = self._conn
        keep = {r[0] for r in c.execute("SELECT serving FROM repos WHERE repo = ? AND serving IS NOT NULL",
                                        (repo,))}
        for f, t in c.execute("SELECT from_version, to_version FROM switches WHERE repo = ?", (repo,)):
            keep |= {v for v in (f, t) if v is not None}
        gone = [vid for (vid,) in c.execute("SELECT id FROM versions WHERE repo = ?", (repo,)) if vid not in keep]
        for vid in gone:
            c.execute("DELETE FROM version_files WHERE version = ?", (vid,))
            c.execute("DELETE FROM overlays WHERE version = ?", (vid,))
            c.execute("DELETE FROM versions WHERE id = ?", (vid,))
        c.commit()
        return len(gone)

    # ── the export ledger ────────────────────────────────────────────────────

    def exported(self, target: str, repo: str) -> dict[str, str]:
        """ident → the digest of the files it went with ('' if not known)."""
        return dict(self._conn.execute(
            "SELECT ident, files FROM exports WHERE target = ? AND repo = ?", (target, repo)))

    def record_export(self, target: str, rows: list, stamp: str) -> None:
        """`rows`: (repo, ident) or (repo, ident, files) — files a digest, or
        the point's {path: checksum} to digest."""
        from mmw.site.tracks import content_ident
        out = []
        for row in rows:
            repo, ident, files = (*row, "")[:3]
            out.append((target, repo, ident, stamp, content_ident(files) if isinstance(files, dict) else files))
        self._conn.executemany("INSERT OR REPLACE INTO exports (target, repo, ident, stamp, files) "
                               "VALUES (?, ?, ?, ?, ?)", out)
        self._conn.commit()

    def forget_export(self, target: str, stamp: str) -> None:
        """Undo one export's record (a bundle that never went): its points
        count as not sent there — they go again, never the other way."""
        self._conn.execute("DELETE FROM exports WHERE target = ? AND stamp = ?", (target, stamp))
        self._conn.commit()

    def export_targets(self) -> list[str]:
        """Every network this site has sent to, the latest first."""
        return [r[0] for r in self._conn.execute(
            "SELECT target FROM exports GROUP BY target ORDER BY MAX(stamp) DESC, target")]

    def point_by_ident(self, track_id: int, ident: str) -> PointInfo | None:
        row = self._conn.execute(f"SELECT {self._P} FROM points WHERE track = ? AND ident = ?",
                                 (track_id, ident)).fetchone()
        return self._point(row) if row else None

    def metas(self, prefix: str) -> dict[str, str]:
        return dict(self._conn.execute("SELECT key, value FROM meta WHERE key LIKE ? || '%'",
                                       (prefix,)).fetchall())

    # ── what each repository serves ──────────────────────────────────────────

    def _state(self, row) -> RepoState:
        repo, label, bases, serving, held, root = row
        return RepoState(repo, label, json.loads(bases),
                         self.version_by_id(serving) if serving is not None else None, bool(held), root)

    def repo_state(self, repo: str) -> RepoState | None:
        row = self._conn.execute("SELECT repo, label, bases, serving, held, root FROM repos WHERE repo = ?",
                                 (repo,)).fetchone()
        return self._state(row) if row else None

    def repo_states(self) -> list[RepoState]:
        return [self._state(r) for r in self._conn.execute(
            "SELECT repo, label, bases, serving, held, root FROM repos ORDER BY lower(label)").fetchall()]

    def set_serving(self, repo: str, version_id: int, held: bool) -> None:
        self._conn.execute("UPDATE repos SET serving = ?, held = ? WHERE repo = ?",
                           (version_id, int(held), repo))
        self._conn.commit()

    def set_root(self, repo: str, root: str | None, *, commit: bool = True) -> None:
        self._conn.execute("UPDATE repos SET root = ? WHERE repo = ?", (root, repo))
        if commit:
            self._conn.commit()

    def set_held(self, repo: str, held: bool) -> None:
        self._conn.execute("UPDATE repos SET held = ? WHERE repo = ?", (int(held), repo))
        self._conn.commit()

    # ── network-level files ──────────────────────────────────────────────────

    def network_files(self) -> dict[str, str]:
        return dict(self._conn.execute(
            "SELECT n.path, b.checksum FROM network_files n JOIN blobs b ON b.id = n.blob").fetchall())

    def set_network_files(self, rows: dict[str, str], sizes: dict[str, int]) -> None:
        c = self._conn
        try:
            c.execute("DELETE FROM network_files")
            c.executemany("INSERT INTO network_files (path, blob) VALUES (?, ?)",
                          [(p, self._blob_id(cs, sizes.get(cs, 0))) for p, cs in rows.items()])
            c.commit()
        except BaseException:
            c.rollback()
            raise

    # ── switches left halfway ────────────────────────────────────────────────

    def begin_switch(self, repo: str, from_id: int | None, to_id: int, overlay: str | None = None,
                     held: bool | None = None) -> None:
        self._conn.execute("INSERT OR REPLACE INTO switches (repo, from_version, to_version, phase, overlay, held) "
                           "VALUES (?, ?, ?, 'pkg', ?, ?)",
                           (repo, from_id, to_id, overlay, None if held is None else int(held)))
        self._conn.commit()

    def switch_held(self, repo: str) -> bool | None:
        """Whether the switch under way leaves the repository held (None: not said)."""
        row = self._conn.execute("SELECT held FROM switches WHERE repo = ?", (repo,)).fetchone()
        return None if row is None or row[0] is None else bool(row[0])

    def switch_overlay(self, repo: str) -> str | None:
        row = self._conn.execute("SELECT overlay FROM switches WHERE repo = ?", (repo,)).fetchone()
        return row[0] if row else None

    def switch_phase(self, repo: str, phase: str) -> None:
        self._conn.execute("UPDATE switches SET phase = ? WHERE repo = ?", (phase, repo))
        self._conn.commit()

    def end_switch(self, repo: str) -> None:
        self._conn.execute("DELETE FROM switches WHERE repo = ?", (repo,))
        self._conn.commit()

    def pending_switches(self) -> list[tuple[str, int | None, int, str]]:
        return [tuple(r) for r in self._conn.execute(
            "SELECT repo, from_version, to_version, phase FROM switches ORDER BY repo").fetchall()]

    # ── log, sizes, references ───────────────────────────────────────────────

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def delete_meta(self, key: str) -> None:
        self._conn.execute("DELETE FROM meta WHERE key = ?", (key,))
        self._conn.commit()

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        self._conn.commit()

    def record_applied(self, created: str, previous: str | None, kind: str) -> None:
        self._conn.execute("INSERT INTO applied (created, previous, kind, applied_at) VALUES (?, ?, ?, ?)",
                           (created, previous, kind, time.strftime("%Y-%m-%dT%H:%M:%S")))
        self._conn.commit()

    def deleted_stamps(self, repo: str) -> list[str]:
        """The bundle stamps of a repository's versions deleted here."""
        old = [s for (s,) in self._conn.execute("SELECT stamp FROM deleted WHERE repo = ?", (repo,))]
        stubs = [s for (s,) in self._conn.execute(
            "SELECT p.stamp FROM points p JOIN tracks t ON t.id = p.track WHERE t.repo = ? AND p.held = 0",
            (repo,))]
        return old + [s for s in stubs if s not in old]

    def applied(self) -> list[tuple[str, str | None, str, str]]:
        """Newest first."""
        return [tuple(r) for r in self._conn.execute(
            "SELECT created, previous, kind, applied_at FROM applied ORDER BY id DESC").fetchall()]

    def version_sizes(self, version_id: int) -> tuple[int, int]:
        """(bytes the version's files take, bytes only it uses — what deleting
        it frees; network-level files count as used)."""
        total = self._conn.execute(
            "SELECT COALESCE(SUM(b.size), 0) FROM blobs b WHERE b.id IN "
            "(SELECT blob FROM version_files WHERE version = ?)", (version_id,)).fetchone()[0]
        only = self._conn.execute(
            "SELECT COALESCE(SUM(b.size), 0) FROM blobs b WHERE b.id IN "
            "(SELECT blob FROM version_files WHERE version = ?) "
            "AND b.id NOT IN (SELECT blob FROM version_files WHERE version != ?) "
            "AND b.id NOT IN (SELECT blob FROM network_files)", (version_id, version_id)).fetchone()[0]
        return total, only

    # ── site layers ──────────────────────────────────────────────────────────

    def layer(self, repo: str) -> SiteLayer | None:
        row = self._conn.execute("SELECT repo, signing, key, contents, drop_valid_until FROM site_layers "
                                 "WHERE repo = ?", (repo,)).fetchone()
        return SiteLayer(row[0], row[1], row[2], bool(row[3]), bool(row[4])) if row else None

    def set_layer(self, repo: str, *, signing: str = "upstream", key: str | None = None,
                  contents: bool = False, drop_valid_until: bool = False) -> None:
        self._conn.execute("INSERT OR REPLACE INTO site_layers VALUES (?, ?, ?, ?, ?)",
                           (repo, signing, key, int(contents), int(drop_valid_until)))
        self._conn.commit()

    def drop_layer(self, repo: str) -> None:
        self._conn.execute("DELETE FROM site_layers WHERE repo = ?", (repo,))
        self._conn.commit()

    def put_overlay(self, repo: str, version_id: int, digest: str,
                    rows: Iterable[tuple[str, str, int]]) -> None:
        """A version's overlay; any other one of the same version is dropped
        unless it is the one being served (a switch still needs it)."""
        c = self._conn
        served = self.repo_overlay(repo)
        c.execute("DELETE FROM overlays WHERE repo = ? AND version = ? AND digest NOT IN (?, ?)",
                  (repo, version_id, digest, served or ""))
        c.execute("DELETE FROM overlays WHERE repo = ? AND version = ? AND digest = ?",
                  (repo, version_id, digest))
        c.executemany("INSERT INTO overlays VALUES (?, ?, ?, ?, ?, ?)",
                      [(repo, version_id, digest, p, cs, size) for p, cs, size in rows])
        c.commit()

    def retire_overlays(self, repo: str, version_id: int, digest: str | None) -> None:
        """Keep only the overlay being served (their blobs are GC roots)."""
        self._conn.execute("DELETE FROM overlays WHERE repo = ? AND NOT (version = ? AND digest = ?)",
                           (repo, version_id, digest or ""))
        self._conn.commit()

    def has_overlay(self, repo: str, version_id: int, digest: str) -> bool:
        return self._conn.execute("SELECT 1 FROM overlays WHERE repo = ? AND version = ? AND digest = ? "
                                  "LIMIT 1", (repo, version_id, digest)).fetchone() is not None

    def overlay_files(self, repo: str, version_id: int, digest: str) -> dict[str, str]:
        return dict(self._conn.execute(
            "SELECT path, checksum FROM overlays WHERE repo = ? AND version = ? AND digest = ?",
            (repo, version_id, digest)).fetchall())

    def served_files(self, version_id: int, digest: str | None) -> dict[str, str]:
        """What serving `version_id` with overlay `digest` puts in the root."""
        files = self.version_files(version_id)
        if digest:
            v = self.version_by_id(version_id)
            for path, cs in self.overlay_files(v.repo, version_id, digest).items():
                if cs:
                    files[path] = cs
                else:
                    files.pop(path, None)
        return files

    def repo_overlay(self, repo: str) -> str | None:
        row = self._conn.execute("SELECT overlay FROM repos WHERE repo = ?", (repo,)).fetchone()
        return row[0] if row else None

    def set_overlay(self, repo: str, digest: str | None, *, commit: bool = True) -> None:
        self._conn.execute("UPDATE repos SET overlay = ? WHERE repo = ?", (digest, repo))
        if commit:
            self._conn.commit()

    def referenced_checksums(self, repos: "set[str] | None" = None, network: bool = True,
                             network_prefix: str | None = None) -> set[str]:
        """Every blob the given repositories' versions use (None: every
        repository), plus the network files (all, or only those under
        `network_prefix`, or none)."""
        out: set[str] = set()
        if repos is None:
            out |= {r[0] for r in self._conn.execute(
                "SELECT checksum FROM blobs WHERE id IN (SELECT blob FROM version_files)").fetchall()}
        elif repos:
            ph = ",".join("?" * len(repos))
            out |= {r[0] for r in self._conn.execute(
                f"SELECT checksum FROM blobs WHERE id IN (SELECT f.blob FROM version_files f "
                f"JOIN versions v ON v.id = f.version WHERE v.repo IN ({ph}))", tuple(repos)).fetchall()}
        # A held point's files are kept, served or not.
        if repos is None:
            out |= {r[0] for r in self._conn.execute(
                "SELECT checksum FROM blobs WHERE id IN (SELECT f.blob FROM point_files f "
                "JOIN points p ON p.id = f.point WHERE p.held = 1)").fetchall()}
        elif repos:
            ph = ",".join("?" * len(repos))
            out |= {r[0] for r in self._conn.execute(
                f"SELECT checksum FROM blobs WHERE id IN (SELECT f.blob FROM point_files f "
                f"JOIN points p ON p.id = f.point JOIN tracks t ON t.id = p.track "
                f"WHERE p.held = 1 AND t.repo IN ({ph}))", tuple(repos)).fetchall()}
        if network:
            out |= {c for p, c in self.network_files().items()
                    if network_prefix is None or p.startswith(network_prefix)}
        # A site layer's files are served too: its debs and rebuilt indexes.
        if repos is None:
            out |= {r[0] for r in self._conn.execute("SELECT checksum FROM overlays WHERE checksum != ''")}
        elif repos:
            ph = ",".join("?" * len(repos))
            out |= {r[0] for r in self._conn.execute(
                f"SELECT checksum FROM overlays WHERE checksum != '' AND repo IN ({ph})", tuple(repos))}
        return out
