-- SiteDB schema and initialization from release v1.3.0.

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
CREATE TABLE IF NOT EXISTS applied (
    id INTEGER PRIMARY KEY, created TEXT NOT NULL, previous TEXT, kind TEXT NOT NULL,
    applied_at TEXT NOT NULL);

ALTER TABLE repos ADD COLUMN root TEXT;
