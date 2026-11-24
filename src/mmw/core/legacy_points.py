"""History from before points, owned by its repository.

Snapshots taken before repository points were converted into *legacy*
points: one network's walk of a repository, its files and nothing more, so
only that network could select them. Here each held legacy point of an apt
repository is split by the units its network selected then — a unit's own
indexes and the pool files they name are that unit's, the rest of the suite
is shared — and recorded as an ordinary repository point. Any network that
selects the repository can then send it, sliced to its own selection.
"""
from __future__ import annotations

import json
import threading

UPGRADE = "legacy-points"
SENT = "sent"
ONE_MODEL = "one-model"
OLD_TABLES = "old-tables"
# The one history upgrade chain, in order; each step marked in `history_upgrades`.
CHAIN = ("manual-snapshots", UPGRADE, SENT, ONE_MODEL, OLD_TABLES)
# What the network walk kept before history belonged to repositories: its
# per-network cache of a repository's points, and the live layer overlay.
# Read by the steps before (`_one_model`), dropped after.
_DROPPED = ("network_repo_rows", "layer_rows", "layer_digests")
_CHAIN_LOCK = threading.Lock()      # one run per process (the TUI's worker and a page's open)
_APT = ("apt", "apt-flat", "apt-local")
_SIGNATURES = ("Release", "InRelease", "Release.gpg")
_INDEX_STEMS = ("Packages", "Sources", "Translation", "Contents")


def upgrade(pool) -> list[str]:
    """Every older history this pool holds, converted at once, every network:
    snapshots become points and recordings, apt legacy points their
    repository's, format-2 transfer records sent points, and what is left
    one network's own (scoped) point. Each step runs once. What it said."""
    pool.flush_writes()
    if CHAIN_DONE <= done(pool):
        return []
    with _CHAIN_LOCK:
        _retire_manual_snapshots(pool)
        notes = list(promote_legacy_points(pool))
        _migrate_sent(pool)
        notes += _one_model(pool)
        _drop_old_tables(pool)
    return notes


def done(pool) -> set[str]:
    """The chain's steps this pool has run."""
    import sqlite3
    with pool._read() as c:
        try:
            return {r[0] for r in c.execute("SELECT name FROM history_upgrades")}
        except sqlite3.OperationalError:            # a read-only open of a pool from before upgrades
            return set()


CHAIN_DONE = set(CHAIN)


def _mark(pool, name: str) -> None:
    with pool._lock:
        pool._conn.execute("INSERT OR IGNORE INTO history_upgrades VALUES (?)", (name,))
        pool._conn.commit()


def _migrate_sent(pool) -> None:
    if SENT in done(pool):
        return
    for network in sorted(pool.network_names()):
        migrate_sent(pool, network)
    _mark(pool, SENT)


def _drop_old_tables(pool) -> None:
    """Once, last: the tables only the older steps read."""
    if OLD_TABLES in done(pool):
        return
    with pool._lock:
        c = pool._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            if not c.execute("SELECT 1 FROM history_upgrades WHERE name = ?", (OLD_TABLES,)).fetchone():
                for table in _DROPPED:
                    c.execute(f"DROP TABLE IF EXISTS {table}")
                c.execute("INSERT OR IGNORE INTO history_upgrades VALUES (?)", (OLD_TABLES,))
            c.commit()
        except BaseException:
            c.rollback()
            raise


def _retire_manual_snapshots(pool) -> None:
    """One-time upgrade: snapshots become ordinary retained point history.

    Leave the old lists/settings for transfer migration. Only release a
    snapshot's independent GC hold after all its files are in held points;
    an unreadable/incomplete conversion keeps its old protection.
    """
    if pool._conn.execute("SELECT 1 FROM history_upgrades WHERE name = 'manual-snapshots'").fetchone():
        return
    from mmw.core.repository_points import files_for_settings
    for network in sorted(pool.network_names()):
        convert_snapshots(pool, network)
        seed_recordings(pool, network)
        deployments = pool.published(network)
        # Older live/as-of publish records contain no exact point IDs.
        # Preserve pre-existing history conservatively until that root is
        # republished, when the normal path records its exact projection.
        existing = pool.points_of(pool.linked_held_points())
        existing.update({p.id: p for p in pool.repository_points()})
        for deployment in deployments:
            if deployment.target != 'live' and not deployment.target.startswith('as-of:'):
                continue
            ids = [p.id for p in existing.values() if p.held and p.stamp <= deployment.made]
            if ids:
                with pool._lock:
                    pool._conn.executemany('INSERT OR IGNORE INTO published_points VALUES (?, ?)',
                                           [(deployment.root, pid) for pid in ids])
                    pool._conn.commit()
        for snap in pool.snapshots(network):
            if not snap.complete:
                continue
            ids = pool.snapshot_point_ids(snap.id)
            try:
                recorded = {p: cs for pid in ids
                            for p, cs in files_for_settings(pool, pid, snap.settings).items()}
            except (ValueError, LookupError):
                continue  # Preserve an old state whose selection cannot be read.
            if not ids or recorded != dict(pool.snapshot_files(snap.id)):
                continue
            for deployment in deployments:
                if deployment.target == snap.stamp:
                    with pool._lock:
                        pool._conn.executemany('INSERT OR IGNORE INTO published_points VALUES (?, ?)',
                                               [(deployment.root, pid) for pid in ids])
                        pool._conn.commit()
            pool.demote_snapshot(snap.id)
    with pool._lock:
        pool._conn.execute("INSERT OR IGNORE INTO history_upgrades VALUES ('manual-snapshots')")
        pool._conn.commit()


def convert_snapshots(pool, network: str, warnings: list[str] | None = None) -> int:
    """Once per network: every complete snapshot taken before points, oldest
    first, split into points — linked to the network at the snapshot's own
    stamp and listed for the snapshot. History-only (imported) snapshots
    have no files to split. One that cannot be split is said in `warnings`
    and skipped — it stays a label — so it never stops the network's points.
    How many were converted."""
    if pool.points_converted(network):
        return 0
    from mmw.core.points import split_rows
    # Inside the upgrade chain: legacy, for the later steps to convert.
    # After it (a snapshot nothing converted yet): the network's own point.
    scope = network if ONE_MODEL in done(pool) else None
    converted = 0
    for snap in reversed(pool.snapshots(network)):
        if not snap.complete:
            continue
        rows = dict(pool.snapshot_files(snap.id))
        try:
            drafts = split_rows(pool, rows, snap.settings, network, snap.stamp)
        except Exception as e:  # noqa: BLE001 — one old snapshot never blocks the rest
            if warnings is not None:
                warnings.append(f"snapshot {snap.stamp} of {network} could not be split into points ({e}) "
                                "— it stays a label; dates before the next one fall back to it")
            continue
        ids = []
        for repo, draft in drafts:
            pid, _new = pool.record_point(repo, draft, snap.stamp, unconverted=scope is None, scope=scope)
            pool.link_network_point(network, pid, snap.stamp)
            ids.append(pid)
        pool.link_snapshot_points(snap.id, ids)
        converted += 1
    pool.mark_points_converted(network)
    return converted


def seed_recordings(pool, network: str, now: str | None = None) -> int:
    """Once per network: each complete snapshot's points, per repository, as
    a recording at its stamp, and its settings — so a date before 3c's first
    recording can still be published. When (`now`) is kept: a date before it
    is only as exact as the snapshots. How many snapshots seeded."""
    if pool.recordings_seeded(network):
        return 0
    from mmw.core.publisher.manifest import now_stamp
    done = 0
    for snap in reversed(pool.snapshots(network)):
        if not snap.complete:
            continue
        ids = pool.snapshot_point_ids(snap.id)
        if not ids:
            continue
        by: dict[str, list[int]] = {}
        for pid in ids:
            t = pool.track_of_point(pid)
            if t is not None:
                by.setdefault(t.repo, []).append(pid)
        last = pool.points_at(network, snap.stamp)
        for repo, pids in by.items():
            if sorted(last.get(repo, [])) != sorted(pids):
                pool.record_recording(network, repo, snap.stamp, pids)
        for repo in set(last) - set(by):
            pool.record_recording(network, repo, snap.stamp, [])
        if snap.settings:
            pool.record_settings(network, snap.stamp, snap.settings)
        done += 1
    pool.mark_recordings_seeded(network, (now or now_stamp()) if done else None)
    return done


def migrate_sent(pool, network: str) -> bool:
    """Once per network: what its format-2 bundles sent (each repository's
    last-sent version) as sent points and files — so the first bundle of
    points is still a delta. Whether it ran."""
    if pool.sent_migrated(network):
        return False
    convert_snapshots(pool, network)
    if pool.full_next(network) is not None:
        # A forced delete said the far side's state is unknown: its history
        # isn't trusted — nothing counts as sent, so the first bundle is full.
        pool.clear_full_next(network)
        pool.mark_sent_migrated(network)
        return True
    for repo, version_id in pool.last_sent_versions(network).items():
        holders = pool.repo_version_snapshots(version_id)
        if not holders:
            continue                    # its snapshot is gone: those files go again (the far side dedupes)
        ids = [pid for pid in pool.snapshot_point_ids(holders[0])
               if (t := pool.track_of_point(pid)) is not None and t.repo == repo]
        files = {c for pid in ids for c in pool.point_files(pid).values()}
        pool.record_sent(network, ids, files, "migrated")
    pool.mark_sent_migrated(network)
    return True


def _one_model(pool) -> list[str]:
    """Every point still legacy becomes the point of the networks that had
    it (`point_scopes`), in place: ids, idents and every row naming it stay.
    One no network had, and nothing names, goes, and is said."""
    if ONE_MODEL in done(pool):
        return []
    from mmw.core.points import AS_OF
    from mmw.site.versionref import day
    # As-of deployments from before exact publish records hold what their
    # date's recordings name, exactly (retention read it so until now).
    for network in sorted(pool.network_names()):
        for deployment in pool.published(network):
            if not deployment.target.startswith(AS_OF):
                continue
            ids = sorted({pid for ids in pool.points_at(network, deployment.target[len(AS_OF):]).values()
                          for pid in ids})
            with pool._lock:
                pool._conn.executemany("INSERT OR IGNORE INTO published_points VALUES (?, ?)",
                                       [(deployment.root, pid) for pid in ids])
                pool._conn.commit()
    notes: list[str] = []
    with pool._lock:
        c = pool._conn
        c.execute("BEGIN IMMEDIATE")
        try:
            if c.execute("SELECT 1 FROM history_upgrades WHERE name = ?", (ONE_MODEL,)).fetchone():
                c.rollback()                         # another process converted it meanwhile
                return []
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            legacy = c.execute("SELECT p.id, t.repo, t.name, t.suite, p.date, p.track, p.ident, p.files, p.held "
                               "FROM points p JOIN tracks t ON t.id = p.track WHERE p.legacy = 1 ORDER BY p.id").fetchall()
            scopes: dict[int, set[str]] = {}

            def add(rows):
                for pid, network in rows:
                    scopes.setdefault(pid, set()).add(network)
            add(c.execute("SELECT point, network FROM network_points"))
            add(c.execute("SELECT sp.point, s.network FROM snapshot_points sp JOIN snapshots s ON s.id = sp.snapshot"))
            add(c.execute("SELECT point, network FROM sent_points"))
            add(c.execute("SELECT pp.point, d.network FROM published_points pp JOIN published d ON d.root = pp.root"))
            named: set[int] = set()
            for table in ("network_recordings", "network_repo_rows"):
                if table in tables:
                    for network, raw in c.execute(f"SELECT network, points FROM {table}").fetchall():
                        add((pid, network) for pid in json.loads(raw))
            if "repository_current" in tables:
                named |= {pid for (raw,) in c.execute("SELECT points FROM repository_current") for pid in json.loads(raw)}
            named |= {r[0] for r in c.execute("SELECT point FROM point_labels")}
            for table in ("network_slices", "network_slice_dates", "sent_slices"):
                if table in tables:
                    named |= {r[0] for r in c.execute(f"SELECT point FROM {table}")}
            for pid, repo, name, suite, date, track, ident, files, held in legacy:
                mine = scopes.get(pid, set())
                if repo.startswith("network:"):
                    mine = mine | {repo.split(":", 1)[1]}
                if not mine:
                    if pid in named:
                        continue                     # named, yet no network had it: left as it was
                    for table in ("point_files", "point_units", "point_unit_files", "point_scopes"):
                        c.execute(f"DELETE FROM {table} WHERE point = ?", (pid,))
                    c.execute("DELETE FROM points WHERE id = ?", (pid,))
                    if held:                         # a stub (one promoted, or pruned) held nothing
                        what = suite or (name.rsplit("/", 1)[-1] if name else "files")
                        notes.append((repo, f"{repo} {what} {day(date)}: no network had it — removed"))
                    continue
                same = c.execute("SELECT id FROM points p WHERE track = ? AND ident = ? AND files = ? AND legacy = 0 "
                                 "AND EXISTS (SELECT 1 FROM point_scopes s WHERE s.point = p.id)",
                                 (track, ident, files)).fetchone()
                c.executemany("INSERT OR IGNORE INTO point_scopes VALUES (?, ?)",
                              [(same[0] if same else pid, n) for n in sorted(mine)])
                if same is not None:                 # already one network's: they are one point
                    move_point_rows(c, pid, same[0])
                    for table in ("point_files", "point_units", "point_unit_files", "point_scopes"):
                        c.execute(f"DELETE FROM {table} WHERE point = ?", (pid,))
                    c.execute("DELETE FROM points WHERE id = ?", (pid,))
                    continue
                if c.execute("SELECT 1 FROM points WHERE track = ? AND ident = ? AND files = ? AND legacy = 0",
                             (track, ident, files)).fetchone():
                    c.execute("DELETE FROM point_scopes WHERE point = ?", (pid,))
                    continue                         # its repository's point has these very files: left as it was
                c.execute("UPDATE points SET legacy = 0 WHERE id = ?", (pid,))
            c.execute("CREATE TABLE IF NOT EXISTS legacy_notes (repo TEXT NOT NULL, note TEXT NOT NULL)")
            c.executemany("INSERT INTO legacy_notes VALUES (?, ?)", notes)
            c.execute("DROP TABLE IF EXISTS published_legacy_holds")
            c.execute("INSERT OR IGNORE INTO history_upgrades VALUES (?)", (ONE_MODEL,))
            c.commit()
        except BaseException:
            c.rollback()
            raise
    return [n for _, n in notes]


def promote_legacy_points(pool) -> list[str]:
    """Once: every held legacy point of an apt repository becomes a
    repository point, its network rows moved to it. One that can't be split
    stays legacy and is said. The warnings."""
    with pool._read() as c:
        if c.execute("SELECT 1 FROM history_upgrades WHERE name = ?", (UPGRADE,)).fetchone():
            return []
        # Suites first: files no Release claims go last, and stay with a
        # suite of their snapshot that couldn't be split (they are its
        # remnants — an unreadable index's packages among them).
        legacy = [r[0] for r in c.execute(
            "SELECT p.id FROM points p JOIN tracks t ON t.id = p.track WHERE p.legacy = 1 AND p.held = 1 "
            "ORDER BY t.name = '', p.id")]
    warnings: list[str] = []
    kept: set[tuple[str, str]] = set()            # (repo, stamp) with a suite kept legacy
    for old in legacy:
        track, stamp = pool.track_of_point(old), pool.points_of([old])[old].stamp
        if track is not None and not track.name and (track.repo, stamp) in kept:
            continue
        try:
            note = _promote(pool, old)
        except Exception as e:  # noqa: BLE001 — one point never blocks the rest, nor opening the pool
            try:
                note = _say(pool, old, str(e))
            except Exception:  # noqa: BLE001
                note = None
        if note:
            warnings.append(note)
            if track is not None:
                kept.add((track.repo, stamp))
    with pool._lock:
        pool._conn.execute("CREATE TABLE IF NOT EXISTS legacy_notes (repo TEXT NOT NULL, note TEXT NOT NULL)")
        pool._conn.executemany("INSERT INTO legacy_notes VALUES (?, ?)",
                               [(n.split(" ", 1)[0], n) for n in warnings])
        pool._conn.execute("INSERT OR IGNORE INTO history_upgrades VALUES (?)", (UPGRADE,))
        pool._conn.commit()
    return warnings


def notes(pool, repos) -> list[str]:
    """What the upgrade said of these repositories' history it kept as one network's."""
    with pool._read() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'legacy_notes'").fetchone():
            return []
        return [n for r, n in c.execute("SELECT repo, note FROM legacy_notes ORDER BY rowid") if r in set(repos)]


def _say(pool, pid: int, _why: str = "") -> str:
    from mmw.site.versionref import day
    point = pool.points_of([pid])[pid]
    track = pool.track_of_point(pid)
    network = _networks(pool, pid)[0][0] if _networks(pool, pid) else "its network"
    suite = track.suite or track.codename or (track.name.rsplit("/", 1)[-1] if track.name else "files")
    return f"{track.repo} {suite} {day(point.date)}: kept as {network}'s only — its indexes can't be read"


def _networks(pool, pid: int) -> list[tuple[str, str]]:
    with pool._read() as c:
        return c.execute("SELECT network, stamp FROM network_points WHERE point = ? ORDER BY stamp, network",
                         (pid,)).fetchall()


def _settings(pool, pid: int) -> str | None:
    """The selection recorded with the point: its network's at the moment it
    was linked, else a snapshot's that holds it."""
    for network, stamp in _networks(pool, pid):
        if settings := pool.settings_at(network, stamp):
            return settings
    with pool._read() as c:
        row = c.execute("SELECT s.settings FROM snapshot_points sp JOIN snapshots s ON s.id = sp.snapshot "
                        "WHERE sp.point = ? AND s.settings IS NOT NULL ORDER BY s.stamp LIMIT 1", (pid,)).fetchone()
    return row[0] if row else None


def _layered(settings: str | None, repo: str) -> bool:
    """Whether the network these settings are of laid a layer over `repo`."""
    from mmw.core.config import NetworkLayer, normalize_url
    # Only the layers: a recorded network as a whole doesn't validate again
    # (its `select` is dumped in a shape the config file refuses).
    layers = (json.loads(settings or "{}").get("network") or {}).get("layers") or []
    for layer in layers:
        try:
            if NetworkLayer.model_validate(layer).identity == normalize_url(repo):
                return True
        except Exception:  # noqa: BLE001 — a layer an older mmw wrote that this one can't read
            continue
    return False


def _kind(path: str, track: str, base: str) -> str:
    from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE
    name = path.rsplit("/", 1)[-1]
    if track and path.startswith(track + "/") and "/" not in path[len(track) + 1:] and name in _SIGNATURES:
        return KIND_RELEASE
    if track and track != base and path.startswith(track + "/"):
        return KIND_META
    if track == base and name.startswith(_INDEX_STEMS):
        return KIND_META
    return KIND_PKG


def _promote(pool, old: int) -> str | None:
    from mmw.core.config import MirrorConfig
    from mmw.core.points import _reader
    from mmw.core.repos import repository_key
    from mmw.core.repository_points import _project
    from mmw.providers import get_provider
    from mmw.site.combine import _SLICE
    from mmw.site.tracks import PointDraft, _indexes, _index_lines, _referenced, content_ident

    track = pool.track_of_point(old)
    if track is None or track.repo.startswith("network:"):
        return None
    settings = _settings(pool, old)
    if _layered(settings, track.repo):
        return None                                   # the network's layer is in it: that network's own
    units = [MirrorConfig.model_validate(u) for u in json.loads(settings or "{}").get("mirrors", [])]
    units = [u for u in units if repository_key(u) == track.repo]
    if not units:
        return _say(pool, old)
    if any(u.provider not in _APT for u in units):
        return None                                   # whole-provider repositories keep their walk
    point = pool.points_of([old])[old]
    files = pool.point_files(old)
    read = _reader(pool, files)
    d = track.name
    if d:
        mine = [u for u in units if d == f"{u.mirror_base}/dists/{u.suite}" or (not u.suite and d == u.mirror_base)]
    else:
        mine = []
    hashed = get_provider(mine[0].provider).by_hash if mine else False
    owned: dict[tuple, dict[str, tuple]] = {}
    claimed: set[str] = set()
    if mine:
        base = mine[0].mirror_base
        indexes = _indexes(files, d)
        for u in mine:
            key = tuple(sorted(u.dims.items()))
            want = "source" if u.arch == "source" else u.arch
            rows: dict[str, tuple] = {}
            for path, cs in files.items():
                if hashed and "/by-hash/" in path:
                    continue                         # generated again from the index it names
                rel = path[len(d) + 1:] if path.startswith(d + "/") else None
                if rel is None:
                    continue
                m = _SLICE.match(rel)
                if m is not None:
                    if m.group(1) == (u.component or m.group(1)) and (m.group(4) or "source") == want:
                        rows[path] = (cs, _kind(path, d, base), hashed)
                elif "/" not in rel or u.component is None or rel.startswith(u.component + "/") \
                        or rel.split("/", 1)[0] not in {x.component for x in mine}:
                    rows[path] = (cs, _kind(path, d, base), hashed)     # suite-wide: Release, i18n …
            for stem, variants in indexes.items():
                if not any(v in rows for v in variants):
                    continue
                for v in variants:
                    data = read(v)
                    if data is None:
                        continue
                    try:
                        names = _referenced(_index_lines(data, v[len(stem):]), stem.rsplit("/", 1)[-1], base)
                    except Exception:  # noqa: BLE001 — a damaged variant (lzma, gzip, bz2): try the next
                        continue
                    rows.update({p: (files[p], _kind(p, d, base), hashed) for p in names if p in files})
                    break
                else:
                    return _say(pool, old)
            owned[key] = rows
            claimed |= set(rows)
    shared = {p: (cs, "meta", False) for p, cs in files.items()
              if p not in claimed and not (hashed and "/by-hash/" in p)}
    memberships = [(dict(k), rows) for k, rows in owned.items() if rows]
    if shared:
        memberships.append((None, shared))
    projection = _project((p, cs, k, h, dims is None) for dims, rows in memberships
                          for p, (cs, k, h) in rows.items())
    if projection != files:
        return _say(pool, old)                       # it wouldn't be served as it was
    identity = content_ident({json.dumps([dims, p, k, bool(h)], sort_keys=True): cs
                              for dims, rows in memberships for p, (cs, k, h) in rows.items()})
    fields = {k: v for k, v in (("Suite", track.suite), ("Codename", track.codename)) if v}
    draft = PointDraft(d, "state:" + identity, point.date, fields, projection)
    new, _created = pool.record_point(track.repo, draft, point.stamp)
    pool.record_unit_files(new, track.repo, memberships)
    _move(pool, old, new)
    pool.stub_point(old)
    return None


def _move(pool, old: int, new: int) -> None:
    """Every row naming the legacy point names the repository point instead."""
    with pool._lock:
        move_point_rows(pool._conn, old, new)
        pool._conn.commit()


def move_point_rows(c, old: int, new: int) -> None:
    """On connection `c`, uncommitted: every network, snapshot, publish and
    transfer row naming point `old` names `new` instead."""
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "point_scopes" in tables:
        # Only one network's point takes another's networks: a repository
        # point taking them would stop serving every other network.
        if c.execute("SELECT 1 FROM point_scopes WHERE point = ?", (new,)).fetchone():
            c.execute("INSERT OR IGNORE INTO point_scopes SELECT ?, network FROM point_scopes WHERE point = ?",
                      (new, old))
        c.execute("DELETE FROM point_scopes WHERE point = ?", (old,))
    for table in ("network_points", "snapshot_points", "published_points", "network_slices",
                  "network_slice_dates", "sent_slices", "sent_points", "layer_views"):
        if table not in tables:
            continue
        c.execute(f"UPDATE OR IGNORE {table} SET point = ? WHERE point = ?", (new, old))
        c.execute(f"DELETE FROM {table} WHERE point = ?", (old,))
    for table, keys in (("network_recordings", ("network", "repo", "stamp")),
                        ("network_repo_rows", ("network", "repo")),
                        ("repository_current", ("repo",))):
        if table not in tables:
            continue
        for *key, raw in c.execute(f"SELECT {', '.join(keys)}, points FROM {table}").fetchall():
            ids = json.loads(raw)
            if old in ids:
                ids = sorted({new if i == old else i for i in ids})
                c.execute(f"UPDATE {table} SET points = ? WHERE {' AND '.join(f'{k} = ?' for k in keys)}",
                          (json.dumps(ids), *key))
