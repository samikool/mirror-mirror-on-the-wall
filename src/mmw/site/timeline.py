"""Timelines: a repository's points imported, one chosen per track, the
choice built into a state (a version) for the switch.

Merging two histories is a union: a point is one upstream Release, so the
same one from anywhere is the same point, and dates order them. Nothing is
refused for arriving out of order; a point whose files are not all here is
kept, reported incomplete, and not served until they come.
"""
from __future__ import annotations

from dataclasses import dataclass

from mmw.core import disk_io
from mmw.core.snapshots import repo_digests
from mmw.site.tracks import split_tracks


@dataclass
class TrackOutcome:
    track: str
    suite: str | None
    point: int | None
    date: str
    status: str          # new | same | widened | refilled | incomplete | conflict
    missing: int = 0
    note: str = ""


def _reader(store, rows: dict[str, str]):
    def read(path: str) -> bytes | None:
        cs = rows.get(path)
        if cs is None or not store.has(cs):
            return None
        with disk_io.ops.open_rb(store.path(cs)) as fh:
            return fh.read()
    return read


def is_complete(db, store, point_id: int, arrived: set[str] = frozenset(), *, cached: bool = True) -> int:
    """How many of the point's files are not here (0: complete). A point
    found complete is remembered until its files or the store change;
    `cached=False` looks again — what is about to be served always does."""
    if cached and db.known_complete(point_id):
        return 0
    missing = sum(1 for cs in set(db.point_files(point_id).values()) if cs not in arrived and not store.has(cs))
    if not missing:
        db.set_complete(point_id, True)
    return missing


def _suite(fields: dict[str, str], track: str) -> str | None:
    return fields.get("Suite") or fields.get("Codename") or (track.rsplit("/", 1)[-1] if track else None)


def import_rows(db, store, repo: str, label: str, bases: list[str], rows: dict[str, str],
                sizes: dict[str, int], stamp: str, snap_label: str | None,
                arrived: set[str], *, own: bool = True) -> list[TrackOutcome]:
    """Split one repository's complete file list into points and add each
    to its track. `own`: the list is this network's selection, so a suite
    it lacks is no longer served (another network's bundle never retires
    one). Committed as one transaction."""
    out: list[TrackOutcome] = []
    drafts = split_tracks(rows, bases, _reader(store, rows), stamp)
    try:
        if own or db.repo_state(repo) is None:
            db.ensure_repo(repo, label, bases, commit=False)
        for d in drafts:
            tid = db.ensure_track(repo, d.track, d.fields, commit=False)
            pid, status = db.import_point(tid, d, sizes, stamp, snap_label, commit=False)
            suite = _suite(d.fields, d.track)
            note = "; ".join(d.warnings)
            if status.startswith("conflict:"):
                out.append(TrackOutcome(d.track, suite, pid, d.date, "conflict", note=status.split(":", 1)[1]))
                continue
            missing = is_complete(db, store, pid, arrived)
            out.append(TrackOutcome(d.track, suite, pid, d.date, "incomplete" if missing else status,
                                    missing, note))
        if own:
            db.set_present(repo, {d.track for d in drafts}, commit=False)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return out


def choose(db, store, repo: str) -> tuple[dict[int, int], list[str]]:
    """{track id: point id}: the pinned point, else the newest complete one.
    A track with no complete point is left out."""
    chosen: dict[int, int] = {}
    warnings: list[str] = []
    for t in db.tracks(repo):
        if t.gone and t.pinned is None:
            continue                         # no longer in this network's selection
        if t.pinned is not None:
            pinned = db.point(t.pinned)
            if pinned is not None and pinned.held and not is_complete(db, store, pinned.id, cached=False):
                chosen[t.id] = pinned.id
                continue
        # Newest first: the first complete one is it — no need to look further.
        newest = next((p for p in db.points(t.id) if not is_complete(db, store, p.id, cached=False)), None)
        if newest is not None:
            if t.pinned is not None:
                warnings.append(f"{_suite({'Suite': t.suite or ''}, t.name) or 'files'}: the pinned point is not "
                                f"complete here — serving {newest.date[:8]} instead")
            chosen[t.id] = newest.id
    return chosen, warnings


def state_files(db, point_ids) -> tuple[dict[str, str], list[str]]:
    """What serving these points puts in the root (path -> checksum), and
    the paths two of them disagree on (the newer point's wins)."""
    points = sorted((db.point(i) for i in point_ids), key=lambda p: (p.date, p.stamp, p.ident))
    files: dict[str, str] = {}
    warnings: list[str] = []
    for p in points:
        for path, cs in db.point_files(p.id).items():
            if path in files and files[path] != cs:
                warnings.append(f"{path} differs between two suites — serving the one from {p.date[:8]}")
            files[path] = cs
    return files, warnings


def ensure_state(db, store, repo: str, chosen: dict[int, int]) -> tuple[int, list[str]]:
    """The version serving exactly `chosen`, built when new. Two tracks
    naming one path with different files: the newer point's wins."""
    ids = sorted(chosen.values())
    found = db.state(repo, ids)
    if found is not None:
        return found.id, []
    state = db.repo_state(repo)
    points = sorted((db.point(i) for i in ids), key=lambda p: (p.date, p.stamp, p.ident))
    files, warnings = state_files(db, ids)
    sizes = db.blob_sizes(files.values())
    newest = max(points, key=lambda p: p.stamp) if points else None
    digest = repo_digests(list(files.items()), {repo: state.bases})[repo]
    vid = db.add_state(repo, state.label, state.bases, ids, digest,
                       newest.stamp if newest else "", newest.snap_label if newest else None,
                       [(p, cs, sizes.get(cs, 0)) for p, cs in files.items()])
    return vid, warnings


def migrate(db, roots) -> bool:
    """Give a site from before timelines its tracks and points: every kept
    version's files, oldest first, split and imported; a held repository
    pins each track to what it serves. Once."""
    if db.get_meta("timeline"):
        return False
    for st in db.repo_states():
        store = roots.store(roots.of(st))
        for v in sorted(db.versions(st.repo), key=lambda v: v.number):
            rows = db.version_files(v.id)
            sizes = db.blob_sizes(rows.values())
            outcomes = import_rows(db, store, st.repo, st.label, st.bases, rows, sizes, v.stamp,
                                   v.snap_label, set())
            ids = [o.point for o in outcomes if o.point is not None and o.status != "conflict"]
            db.set_state_points(v.id, ids)
            if st.held and st.serving is not None and st.serving.id == v.id:
                for o in outcomes:
                    if o.point is not None and o.status != "conflict":
                        db.set_pin(db.point(o.point).track, o.point)
    db.set_meta("timeline", "1")
    return True
