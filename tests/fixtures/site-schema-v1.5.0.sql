-- SiteDB schema and initialization from release v1.5.0.

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

ALTER TABLE repos ADD COLUMN root TEXT;
ALTER TABLE repos ADD COLUMN overlay TEXT;
ALTER TABLE switches ADD COLUMN overlay TEXT;
ALTER TABLE switches ADD COLUMN held INTEGER;
ALTER TABLE versions ADD COLUMN points TEXT;
ALTER TABLE tracks ADD COLUMN gone INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tracks ADD COLUMN keep TEXT;
ALTER TABLE points ADD COLUMN complete INTEGER;
ALTER TABLE points ADD COLUMN selection TEXT;
