"""One archive under two URLs, made one repository with one history.

`older`'s points move under `newer`'s tracks, their paths re-prefixed from
its base to newer's (a Release never names its host, so checksums are the
same). A point newer already holds — the same Release with the same files —
is kept once. Network and transfer records follow the points; `older`'s
source leaves the config and every network that selected it selects
`newer`. Proof is needed first: the same repository by another URL scheme,
or an identical Release in both. A suite name in common only is asked
about; nothing in common is refused.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class MergePlan:
    older: str
    newer: str
    points: int
    tracks: dict[str, str]
    proof: str | None                 # "identity" | "release" | "fields" | None
    already: bool = False


@dataclass
class MergeResult:
    plan: MergePlan
    moved: int = 0
    same: int = 0
    cfg: object = None
    lines: list[str] = field(default_factory=list)


PROOF_SAID = {
    "identity": "the same repository by another URL scheme",
    "release": "they hold an identical Release",
    "fields": "only their suite names match — check they are one archive",
    "finish": "its history moved already (a merge stopped before the config was saved) — this finishes it",
}


def _base(cfg, repo: str, pool) -> str | None:
    src = next((s for s in cfg.sources if s.identity == repo), None)
    if src is not None:
        units = src.to_mirrors()
        if units:
            return units[0].mirror_base
    with pool._read() as c:
        names = [r[0] for r in c.execute("SELECT name FROM tracks WHERE repo = ? AND name != ''", (repo,))]
    return names[0].split("/dists/", 1)[0] if names else None


def _tracks(pool, repo: str) -> list[tuple[int, str, str | None, str | None]]:
    with pool._read() as c:
        return c.execute("SELECT id, name, suite, codename FROM tracks WHERE repo = ? ORDER BY name",
                         (repo,)).fetchall()


def _releases(pool, repo: str) -> set[str]:
    """The checksums of every Release/InRelease the repository's points hold."""
    with pool._read() as c:
        return {r[0] for r in c.execute(
            "SELECT b.checksum FROM tracks t JOIN points p ON p.track = t.id "
            "JOIN point_files f ON f.point = p.id JOIN snapshot_paths sp ON sp.id = f.path "
            "JOIN snapshot_blobs b ON b.id = f.blob WHERE t.repo = ? AND t.name != '' "
            "AND (sp.path = t.name || '/Release' OR sp.path = t.name || '/InRelease')", (repo,))}


def resolve(cfg, ref: str) -> str:
    """A repository key for what someone typed: a source's name, label or URL."""
    from mmw.core.config import normalize_url
    for s in cfg.sources:
        if ref in (s.name, s.identity) or (s.label and ref.lower() == s.label.lower()):
            return s.identity
    if "://" in ref:
        return normalize_url(ref)
    raise LookupError(f"unknown repository {ref!r} — configured: "
                      + ", ".join(sorted(s.name for s in cfg.sources)))


def plan_merge(pool, cfg, older: str, newer: str) -> MergePlan:
    from mmw.core.repos import rebase_files, repository_identity
    if older == newer:
        raise ValueError("that is one repository already")
    o_tracks = _tracks(pool, older)
    in_config = any(s.identity == older for s in cfg.sources)
    if not o_tracks and not in_config:
        return MergePlan(older, newer, 0, {}, None, already=True)
    if not o_tracks and _tracks(pool, newer):
        return MergePlan(older, newer, 0, {}, "finish")
    old_base, new_base = _base(cfg, older, pool), _base(cfg, newer, pool)
    tracks = {name: next(iter(rebase_files({name: ""}, old_base, new_base))) if old_base and new_base else name
              for _tid, name, _s, _c in o_tracks}
    with pool._read() as c:
        count = sum(c.execute("SELECT count(*) FROM points WHERE track = ?", (tid,)).fetchone()[0]
                    for tid, *_rest in o_tracks)
    if repository_identity(older) == repository_identity(newer):
        proof = "identity"
    elif _releases(pool, older) & _releases(pool, newer):
        proof = "release"
    elif {cn or s for _t, _n, s, cn in o_tracks if cn or s} & \
            {cn or s for _t, _n, s, cn in _tracks(pool, newer) if cn or s}:
        proof = "fields"
    else:
        proof = None
    return MergePlan(older, newer, count, tracks, proof)


def _unit(c, repo: str, dims: str) -> int:
    c.execute("INSERT OR IGNORE INTO units (repo, dims) VALUES (?, ?)", (repo, dims))
    return c.execute("SELECT id FROM units WHERE repo = ? AND dims = ?", (repo, dims)).fetchone()[0]


def _path(c, path: str) -> int:
    c.execute("INSERT OR IGNORE INTO snapshot_paths (path) VALUES (?)", (path,))
    return c.execute("SELECT id FROM snapshot_paths WHERE path = ?", (path,)).fetchone()[0]


def merge_repositories(pool, cfg, older: str, newer: str, *, config_dir: Path | None = None,
                       confirmed: bool = False) -> MergeResult:
    """Fold `older` into `newer` (see the module). Refused without proof;
    a suite-names-only match needs `confirmed`."""
    from mmw.core.legacy_points import move_point_rows
    from mmw.core.repos import rebase_files
    from mmw.site.tracks import content_ident
    plan = plan_merge(pool, cfg, older, newer)
    result = MergeResult(plan, cfg=cfg)
    if plan.already:
        result.lines.append(f"{older} and {newer} are already one repository")
        return result
    if plan.proof is None:
        raise ValueError(f"nothing shows they are one repository ({older}, {newer}): no identical Release, "
                         "no suite in common — merging them would mix two archives' histories")
    if plan.proof == "fields" and not confirmed:
        raise PermissionError(f"{older} and {newer}: {PROOF_SAID['fields']}")
    old_base, new_base = _base(cfg, older, pool), _base(cfg, newer, pool)
    if plan.proof == "finish":
        result.cfg = _merged_config(cfg, older, newer)
        if config_dir is not None:
            from mmw.core import config
            config.save(result.cfg, config_dir)
        result.lines.append(f"{older} is now {newer}: the merge that stopped is finished")
        return result
    with pool._lock:
        c = pool._conn
        try:
            for tid, name, suite, codename in _tracks(pool, older):
                new_name = plan.tracks[name]
                c.execute("INSERT INTO tracks (repo, name, suite, codename) VALUES (?, ?, ?, ?) "
                          "ON CONFLICT(repo, name) DO UPDATE SET suite = coalesce(suite, excluded.suite), "
                          "codename = coalesce(codename, excluded.codename)", (newer, new_name, suite, codename))
                target = c.execute("SELECT id FROM tracks WHERE repo = ? AND name = ?", (newer, new_name)).fetchone()[0]
                for pid, ident, legacy in c.execute("SELECT id, ident, legacy FROM points WHERE track = ?",
                                                    (tid,)).fetchall():
                    files = dict(c.execute("SELECT sp.path, b.checksum FROM point_files f "
                                           "JOIN snapshot_paths sp ON sp.id = f.path JOIN snapshot_blobs b ON b.id = f.blob "
                                           "WHERE f.point = ?", (pid,)).fetchall())
                    moved = rebase_files(files, old_base, new_base) if old_base and new_base else files
                    units = c.execute("SELECT sp.path, b.checksum, f.kind, f.hashed, u.dims, f.unit "
                                      "FROM point_unit_files f JOIN snapshot_paths sp ON sp.id = f.path "
                                      "JOIN snapshot_blobs b ON b.id = f.blob LEFT JOIN units u ON u.id = f.unit "
                                      "WHERE f.point = ?", (pid,)).fetchall()
                    new_ident = ident
                    if units and ident.startswith("state:"):
                        rows = {}
                        for path, cs, kind, hashed, dims, _uid in units:
                            p2 = next(iter(rebase_files({path: cs}, old_base, new_base))) if old_base and new_base else path
                            d = json.loads(dims) if dims else None
                            rows[json.dumps([d, p2, kind, bool(hashed)], sort_keys=True)] = cs
                        new_ident = "state:" + content_ident(rows)
                    digest = content_ident(moved)
                    same = c.execute("SELECT id FROM points WHERE track = ? AND ident = ? AND files = ? AND legacy = ?",
                                     (target, new_ident, digest, legacy)).fetchone()
                    if same is not None:
                        move_point_rows(c, pid, same[0])
                        for table in ("point_files", "point_units", "point_unit_files"):
                            c.execute(f"DELETE FROM {table} WHERE point = ?", (pid,))
                        c.execute("DELETE FROM points WHERE id = ?", (pid,))
                        result.same += 1
                        continue
                    c.execute("UPDATE points SET track = ?, ident = ?, files = ? WHERE id = ?",
                              (target, new_ident, digest, pid))
                    if moved != files:
                        c.execute("DELETE FROM point_files WHERE point = ?", (pid,))
                        c.executemany("INSERT INTO point_files (point, path, blob) SELECT ?, ?, id FROM snapshot_blobs "
                                      "WHERE checksum = ?", [(pid, _path(c, p), cs) for p, cs in moved.items()])
                    if units:
                        c.execute("DELETE FROM point_unit_files WHERE point = ?", (pid,))
                        for path, cs, kind, hashed, dims, uid in units:
                            p2 = next(iter(rebase_files({path: cs}, old_base, new_base))) if old_base and new_base else path
                            nuid = _unit(c, newer, dims) if uid else 0
                            c.execute("INSERT OR IGNORE INTO point_unit_files SELECT ?, ?, ?, id, ?, ? FROM snapshot_blobs "
                                      "WHERE checksum = ?", (pid, nuid, _path(c, p2), kind, hashed, cs))
                    membership = c.execute("SELECT m.unit, sp.path, u.dims FROM point_units m "
                                           "JOIN snapshot_paths sp ON sp.id = m.path LEFT JOIN units u ON u.id = m.unit "
                                           "WHERE m.point = ?", (pid,)).fetchall()
                    if membership:
                        c.execute("DELETE FROM point_units WHERE point = ?", (pid,))
                        for uid, path, dims in membership:
                            p2 = next(iter(rebase_files({path: ""}, old_base, new_base))) if old_base and new_base else path
                            c.execute("INSERT OR IGNORE INTO point_units VALUES (?, ?, ?)",
                                      (pid, _unit(c, newer, dims) if uid else 0, _path(c, p2)))
                    result.moved += 1
                for table in ("transfer_targets", "listed_tracks"):
                    if c.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone():
                        c.execute(f"UPDATE OR IGNORE {table} SET repo = ?, track = ? WHERE repo = ? AND track = ?",
                                  (newer, target, older, tid))
                        c.execute(f"DELETE FROM {table} WHERE repo = ? AND track = ?", (older, tid))
                c.execute("DELETE FROM tracks WHERE id = ?", (tid,))
            for table, keys in (("network_recordings", ("network", "stamp")), ("network_repo_rows", ("network",))):
                for *key, raw in c.execute(f"SELECT {', '.join(keys)}, points FROM {table} WHERE repo = ?",
                                           (older,)).fetchall():
                    where = " AND ".join(f"{k} = ?" for k in keys)
                    have = c.execute(f"SELECT points FROM {table} WHERE repo = ? AND {where}", (newer, *key)).fetchone()
                    ids = sorted(set(json.loads(raw)) | set(json.loads(have[0]) if have else []))
                    if have:
                        c.execute(f"UPDATE {table} SET points = ? WHERE repo = ? AND {where}", (json.dumps(ids), newer, *key))
                        c.execute(f"DELETE FROM {table} WHERE repo = ? AND {where}", (older, *key))
                    else:
                        c.execute(f"UPDATE {table} SET repo = ?, points = ? WHERE repo = ? AND {where}",
                                  (newer, json.dumps(ids), older, *key))
            current = c.execute("SELECT points FROM repository_current WHERE repo = ?", (older,)).fetchone()
            if current and not c.execute("SELECT 1 FROM repository_current WHERE repo = ?", (newer,)).fetchone():
                c.execute("INSERT INTO repository_current VALUES (?, ?)", (newer, current[0]))
            c.execute("DELETE FROM repository_current WHERE repo = ?", (older,))
            c.execute("DELETE FROM repository_failed_units WHERE repo = ?", (older,))
            c.commit()
        except BaseException:
            c.rollback()
            raise
    result.cfg = _merged_config(cfg, older, newer)
    if config_dir is not None:
        from mmw.core import config
        config.save(result.cfg, config_dir)
    result.lines.append(f"{older} is now {newer}: {result.moved} point(s) moved, {result.same} already there")
    return result


def _merged_config(cfg, older: str, newer: str):
    """The config with `older`'s source gone and its networks selecting `newer`."""
    from mmw.core.config import normalize_url
    target = next((s for s in cfg.sources if s.identity == newer), None)
    networks = []
    for n in cfg.networks:
        select = [sel.model_copy(update={"source": target.url if target else newer})
                  if normalize_url(sel.source) == older else sel for sel in n.select]
        networks.append(n.model_copy(update={"select": select}))
    return cfg.model_copy(update={"sources": [s for s in cfg.sources if s.identity != older],
                                  "networks": networks})
