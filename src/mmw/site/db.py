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
        if "overlay" not in {r[1] for r in self._conn.execute("PRAGMA table_info(switches)")}:
            self._conn.execute("ALTER TABLE switches ADD COLUMN overlay TEXT")

    def __enter__(self) -> "SiteDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    # ── interning ────────────────────────────────────────────────────────────

    def _blob_id(self, checksum: str, size: int) -> int:
        c = self._conn
        c.execute("INSERT OR IGNORE INTO blobs (checksum, size) VALUES (?, ?)", (checksum, size))
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
                raise ValueError(f"{label} v{number} here differs from the one in this bundle")
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
        c.execute("DELETE FROM overlays WHERE repo = ?", (repo,))        # its site layer went with it
        c.execute("DELETE FROM site_layers WHERE repo = ?", (repo,))
        c.execute("DELETE FROM meta WHERE key = ?", (f"hold:{repo}",))
        c.commit()

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

    def begin_switch(self, repo: str, from_id: int | None, to_id: int, overlay: str | None = None) -> None:
        self._conn.execute("INSERT OR REPLACE INTO switches (repo, from_version, to_version, phase, overlay) "
                           "VALUES (?, ?, ?, 'pkg', ?)", (repo, from_id, to_id, overlay))
        self._conn.commit()

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

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        self._conn.commit()

    def record_applied(self, created: str, previous: str | None, kind: str) -> None:
        self._conn.execute("INSERT INTO applied (created, previous, kind, applied_at) VALUES (?, ?, ?, ?)",
                           (created, previous, kind, time.strftime("%Y-%m-%dT%H:%M:%S")))
        self._conn.commit()

    def deleted_stamps(self, repo: str) -> list[str]:
        """The bundle stamps of a repository's versions deleted here."""
        return [s for (s,) in self._conn.execute("SELECT stamp FROM deleted WHERE repo = ?", (repo,))]

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
