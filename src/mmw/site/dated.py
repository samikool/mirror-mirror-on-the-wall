"""Dated URLs: every held point of a suite, reachable at a read-only path
beside the live tree — `{base}/@YYYY-MM-DD/` — so a build can pin its
sources.list to a day and get exactly what existed then.

The tree at `@D` is what each suite of the repository had as of D (its
newest held, complete point dated on or before D): those points' files,
hardlinked from the root's store, so it costs inodes, not bytes. nginx
needs nothing new — the trees sit under each base it already serves.

A day is keyed by the ids of the points it is built from, so deciding what
changed reads only the points table; a day's files are read only when it is
built, one day at a time. Every tree mmw builds carries a `.mmw-dated`
marker, and only marked trees are ever removed.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from mmw.core import disk_io

DATED_DIR = re.compile(r"\.?@\d{4}-\d{2}-\d{2}(\.building|\.old)?")
MARKER = ".mmw-dated"


def _day(stamp: str) -> str:
    return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}"


def days(db, store, repo: str) -> dict[str, list[int]]:
    """`YYYY-MM-DD` → the ids of the points `repo`'s tree for that day is
    built from (each suite's newest held, complete point dated on or
    before it), for each day a held, complete point is dated."""
    from mmw.site.timeline import is_complete
    per_track = []
    for t in db.tracks(repo):
        pts = [p for p in db.points(t.id) if p.held and not is_complete(db, store, p.id)]
        if pts:
            per_track.append(sorted(pts, key=lambda p: (p.date, p.stamp)))
    out = {}
    for d in sorted({p.date[:8] for pts in per_track for p in pts}):
        chosen = [max((p for p in pts if p.date[:8] <= d), key=lambda p: (p.date, p.stamp), default=None)
                  for pts in per_track]
        out[_day(d)] = [p.id for p in sorted((c for c in chosen if c is not None), key=lambda p: (p.date, p.stamp))]
    return out


def _files(db, bases: list[str], point_ids: list[int], day: str) -> dict[str, dict[str, str]]:
    """`{base}/@day` → {path under it: checksum}; the newer point's file wins."""
    files: dict[str, str] = {}
    for pid in point_ids:                                        # oldest first
        files.update(db.point_files(pid))
    out: dict[str, dict[str, str]] = {}
    for rel, cs in files.items():
        base = next((b for b in bases if rel.startswith(b + "/")), None)
        if base is not None:
            out.setdefault(f"{base}/@{day}", {})[rel[len(base) + 1:]] = cs
    return out


def _ids_key(point_ids: list[int]) -> str:
    return hashlib.sha256(",".join(map(str, point_ids)).encode()).hexdigest()


def _ours(path: Path) -> bool:
    return (disk_io.ops.is_dir(path) and not disk_io.ops.is_symlink(path)
            and disk_io.ops.is_file(path / MARKER))


def _remove(path: Path) -> bool:
    if _ours(path):
        disk_io.ops.rmtree(path, ignore_errors=True)
        return True
    return False


def _build(root: Path, store, rel_dir: str, files: dict[str, str]) -> None:
    """Built aside, then swapped in: a client never sees half a day."""
    final = root / rel_dir
    tmp = final.with_name(f".{final.name}.building")
    old = final.with_name(f".{final.name}.old")
    disk_io.ops.rmtree(tmp, ignore_errors=True)
    disk_io.ops.mkdir(tmp, parents=True)
    disk_io.ops.write_text(tmp / MARKER, "built by mmw: a dated tree of this repository\n")
    for rel, cs in sorted(files.items()):
        dest = tmp / rel
        disk_io.ops.mkdir(dest.parent, parents=True, exist_ok=True)
        disk_io.ops.hardlink(store.path(cs), dest)
    if _ours(final):
        disk_io.ops.rmtree(old, ignore_errors=True)
        disk_io.ops.replace(final, old)
    disk_io.ops.replace(tmp, final)
    disk_io.ops.rmtree(old, ignore_errors=True)


def refresh(db, roots) -> bool:
    """Make every root's dated trees what its repositories' held points
    want (none with `dated = false`); whether any changed. A tree whose
    files aren't all in the store is left as it was."""
    changed = False
    every_base = {b for st in db.repo_states() for b in st.bases}
    for r in roots.all:
        root = Path(r.path)
        if not disk_io.ops.is_dir(root / ".mmw" / "store"):
            continue                                             # not mounted: left as it is
        store = roots.store(r)
        key = f"dated:{r.name}"
        before = json.loads(db.get_meta(key) or "{}")
        if before and not isinstance(next(iter(before.values())), dict):
            before = {}                                          # an earlier record's shape: rebuilt once
        after: dict[str, dict] = {}
        if roots.settings.dated:
            for repo in sorted(roots.placed_in(db, r)):
                st = db.repo_state(repo)
                bases = sorted(st.bases, key=len, reverse=True)
                for d, ids in days(db, store, repo).items():
                    k = f"{repo}|{d}"
                    ids_key = _ids_key(ids)
                    rec = before.get(k)
                    if rec and rec.get("ids") == ids_key and all(_ours(root / x) for x in rec.get("dirs", [])):
                        after[k] = rec
                        continue
                    trees = _files(db, bases, ids, d)
                    if not all(store.has(cs) for f in trees.values() for cs in set(f.values())):
                        if rec:
                            after[k] = rec                       # a file is gone from the store: keep what is there
                        continue
                    for rel_dir, files in trees.items():
                        _build(root, store, rel_dir, files)
                    after[k] = {"ids": ids_key, "dirs": sorted(trees)}
                    changed = True
        wanted_dirs = {x for rec in after.values() for x in rec["dirs"]}
        for rec in before.values():
            for x in rec.get("dirs", []):
                if x not in wanted_dirs and _remove(root / x):
                    changed = True
        # Marked trees nothing wants — a moved repository's, a build a crash cut short.
        for base in every_base:
            bdir = root / base
            if not disk_io.ops.is_dir(bdir) or disk_io.ops.is_symlink(bdir):
                continue
            for entry in disk_io.ops.iterdir(bdir):
                rel = f"{base}/{entry.name}"
                if DATED_DIR.fullmatch(entry.name) and rel not in wanted_dirs and _remove(entry):
                    changed = True
        if after != before:
            db.set_meta(key, json.dumps(after, sort_keys=True))
    return changed


def urls(db, roots) -> list[tuple[str, str]]:
    """(repository label, `host/path/@YYYY-MM-DD/`) for each dated tree."""
    labels = {s.repo: s.label for s in db.repo_states()}
    out = []
    for r in roots.all:
        for k, rec in sorted(json.loads(db.get_meta(f"dated:{r.name}") or "{}").items()):
            if isinstance(rec, dict):
                out += [(labels.get(k.split("|", 1)[0], "?"), x + "/") for x in rec.get("dirs", [])]
    return sorted(out, key=lambda x: (x[0], x[1]))
