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

UPGRADE = "legacy-points"
_APT = ("apt", "apt-flat", "apt-local")
_SIGNATURES = ("Release", "InRelease", "Release.gpg")
_INDEX_STEMS = ("Packages", "Sources", "Translation", "Contents")


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
        except (OSError, ValueError, LookupError) as e:            # one point never blocks the rest
            note = _say(pool, old, str(e))
        if note:
            warnings.append(note)
            if track is not None:
                kept.add((track.repo, stamp))
    with pool._lock:
        pool._conn.execute("INSERT OR IGNORE INTO history_upgrades VALUES (?)", (UPGRADE,))
        pool._conn.commit()
    return warnings


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
                    names = _referenced(_index_lines(data, v[len(stem):]), stem.rsplit("/", 1)[-1], base)
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
    for table in ("network_points", "snapshot_points", "published_points", "network_slices",
                  "network_slice_dates", "sent_slices", "sent_points"):
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
