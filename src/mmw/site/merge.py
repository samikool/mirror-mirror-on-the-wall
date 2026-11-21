"""One archive held here under two URLs, made one repository.

The far side's half of `mmw merge` (see `mmw.core.repo_merge`): `older`'s
points join `newer`'s tracks, their paths re-prefixed from older's base to
newer's (a point is its upstream Release, so an identical one is kept once,
or widened); `newer` serves what its timeline then chooses; and only then
do `older`'s paths stop being served and `older` is forgotten. Proof first,
as there: another URL scheme, or an identical Release — a suite name in
common only is asked about, nothing in common refused.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from mmw.core.repo_merge import PROOF_SAID


@dataclass(frozen=True)
class SiteMergePlan:
    older: str
    newer: str
    points: int
    tracks: dict[str, str]
    proof: str | None
    already: bool = False


@dataclass
class SiteMergeResult:
    plan: SiteMergePlan
    moved: int = 0
    same: int = 0
    lines: list[str] = field(default_factory=list)


def _plan(db, older: str, newer: str) -> SiteMergePlan:
    from mmw.core.repos import rebase_files, repository_identity, same_release_identity
    o, n = db.repo_state(older), db.repo_state(newer)
    if o is None:
        return SiteMergePlan(older, newer, 0, {}, None, already=True)
    old_base = o.bases[0] if o.bases else ""
    new_base = n.bases[0] if n.bases else old_base
    o_tracks, n_tracks = db.tracks(older), db.tracks(newer)
    tracks = {t.name: next(iter(rebase_files({t.name: ""}, old_base, new_base))) if t.name else ""
              for t in o_tracks}
    idents = lambda ts: {p.ident for t in ts for p in db.points(t.id, stubs=True)}  # noqa: E731
    names = lambda ts: {t.codename or t.suite for t in ts if t.codename or t.suite}  # noqa: E731
    if repository_identity(older) == repository_identity(newer):
        proof = "identity"
    elif same_release_identity(idents(o_tracks), idents(n_tracks)):
        proof = "release"
    elif names(o_tracks) & names(n_tracks):
        proof = "fields"
    else:
        proof = None
    count = sum(len(db.points(t.id, stubs=True)) for t in o_tracks)
    return SiteMergePlan(older, newer, count, tracks, proof)


def plan(home: Path, older: str, newer: str) -> SiteMergePlan:
    """What merging would do — read only. An `older` no longer here (merged
    already) is `already`."""
    from mmw.site.db import SiteDB
    from mmw.site.ops import resolve_repo
    with SiteDB(home) as db:
        n = resolve_repo(db, newer)
        try:
            o = resolve_repo(db, older)
        except LookupError:
            return SiteMergePlan(older, n, 0, {}, None, already=True)
        if o == n:
            raise ValueError("that is one repository already")
        return _plan(db, o, n)


def describe(p: SiteMergePlan) -> str:
    moves = ", ".join(f"{a} → {b}" for a, b in sorted(p.tracks.items()) if a != b) or "the same paths"
    return (f"{p.older} into {p.newer}: {p.points} point(s), {moves} — "
            f"{PROOF_SAID.get(p.proof, 'nothing shows they are one repository')}")


def merge_site_repositories(home: Path, older: str, newer: str, *, dry_run: bool = False,
                            confirmed: bool = False) -> SiteMergeResult:
    from mmw.core import disk_io
    from mmw.core.repos import rebase_files
    from mmw.core.served_tree import _prune_empty_parents
    from mmw.site.ops import _render, _reserve, _session
    from mmw.site.tracks import PointDraft
    p = plan(home, older, newer)
    result = SiteMergeResult(p)
    if p.already:
        result.lines.append(f"{older} and {newer} are already one repository")
        return result
    if p.proof is None:
        raise ValueError(f"nothing shows they are one repository ({p.older}, {p.newer}): no identical Release, "
                         "no suite in common — merging them would mix two archives' histories")
    if p.proof == "fields" and not confirmed:
        raise PermissionError(f"{p.older} and {p.newer}: {PROOF_SAID['fields']}")
    if dry_run:
        return result
    with _session(home) as (db, roots):
        o, n = db.repo_state(p.older), db.repo_state(p.newer)
        if roots.of(o).name != roots.of(n).name:
            raise ValueError(f"{o.label} and {n.label} are in different roots — move one beside the other first "
                             "(mmw site move)")
        old_base = o.bases[0] if o.bases else ""
        new_base = n.bases[0] if n.bases else old_base
        had = {t.name for t in db.tracks(p.newer)}
        carry = []                                # (new track, old track, old point → new point, new to newer)
        for t in db.tracks(p.older):
            mapped: dict[int, int] = {}
            tid = db.ensure_track(p.newer, p.tracks[t.name],
                                  {k: v for k, v in (("Origin", t.origin), ("Label", t.label), ("Suite", t.suite),
                                                     ("Codename", t.codename)) if v}, commit=False)
            for point in db.points(t.id, stubs=True):
                files = rebase_files(db.point_files(point.id), old_base, new_base)
                sizes = db.blob_sizes(files.values())
                draft = PointDraft(p.tracks[t.name], point.ident, point.date, {}, files)
                pid, status = db.import_point(tid, draft, sizes, point.stamp, point.snap_label, commit=False)
                mapped[point.id] = pid
                if status.startswith("conflict:"):
                    db.rollback()
                    raise ValueError(f"{status.split(':', 1)[1]} differs between {o.label} and {n.label} inside one "
                                     "Release — the store or one of them is damaged; mmw site verify")
                if status == "new":
                    result.moved += 1
                    if not point.held:
                        db.stub_point(pid)
                else:
                    result.same += 1
            carry.append((tid, t, mapped, p.tracks[t.name] not in had))
        db.commit()
        # What older had retired stays retired, and what it pinned stays pinned.
        for tid, t, mapped, fresh in carry:
            networks = db.track_networks(t.id)
            if fresh:
                db.restore_track_presence(tid, networks, t.gone)
            elif networks:
                have = db.track_networks(tid)
                db.restore_track_presence(tid, {**networks, **have}, db.track(tid).gone)
                db._update_track_retirement(p.newer)
                db.commit()
            if t.pinned in mapped and db.track(tid).pinned is None:
                db.set_pin(tid, mapped[t.pinned])
        switched = _reserve(db, roots, p.newer)
        served_new = set(db.served_files(db.repo_state(p.newer).serving.id, db.repo_overlay(p.newer))) \
            if db.repo_state(p.newer).serving else set()
        root = roots.of(o)
        if o.serving is not None and db.repo_state(p.newer).serving is not None:
            network = set(db.network_files())
            for rel in sorted(set(db.served_files(o.serving.id, db.repo_overlay(p.older))) - served_new - network):
                target = Path(root.path) / rel
                if disk_io.ops.remove(target) is not None:
                    _prune_empty_parents(str(root.path), str(target.parent))
        db.drop_repo(p.older)
        _render(roots, db)
        result.lines.append(f"{o.label} is now {n.label}: {result.moved} point(s) moved, {result.same} already there")
        result.lines.extend(switched.warnings)
    return result
