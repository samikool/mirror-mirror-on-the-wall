"""What an operator does to a site between bundles: serve any kept version
of a repository, hold it where it is, move it forward."""
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from pathlib import Path

from mmw.core import disk_io
from mmw.core.repos import resolve_repo_refs
from mmw.core.served_tree import render_derived
from mmw.site.db import PointInfo, SiteDB, TrackInfo, VersionInfo

if TYPE_CHECKING:
    from mmw.site.db import RepoState
from mmw.site.home import load_site
from mmw.site.lock import SiteLock
from mmw.site.store import Store
from mmw.site.roots import Roots
from mmw.site.switch import SwitchResult, resume_switches, switch_in
from mmw.site.timeline import migrate


def resolve_repo(db: SiteDB, ref: str) -> str:
    """A repository of this site by key, or by label in any case."""
    return resolve_repo_refs([ref], {s.repo: s.label for s in db.repo_states()})[0]


# ── timelines: naming points, choosing, serving ──────────────────────────────

def suite_of(track: TrackInfo) -> str:
    return track.suite or track.codename or (track.name.rsplit("/", 1)[-1] if track.name else "files")


def _track_named(db: SiteDB, repo: str, name: str) -> TrackInfo:
    tracks = db.tracks(repo)
    want = name.strip().lower()
    for t in tracks:
        if want in {x.lower() for x in (t.suite, t.codename, t.name, suite_of(t)) if x}:
            return t
    raise LookupError(f"{db.repo_state(repo).label} has no suite {name!r} — it has "
                      f"{', '.join(suite_of(t) for t in tracks) or 'none'}")


def _targets(db: SiteDB, repo: str, when: str, track: str | None, *,
             as_of: bool) -> list[tuple[TrackInfo, PointInfo]]:
    """The point(s) `when` names: in one suite (`track`), else in every suite
    that has one. A date with `as_of`: each suite's newest on or before it."""
    from mmw.site.versionref import as_of_points, is_when, resolve_point
    label = db.repo_state(repo).label
    tracks = [_track_named(db, repo, track)] if track else [t for t in db.tracks(repo) if db.points(t.id)]
    dated = as_of and is_when(when)
    if len(tracks) == 1 and not dated:
        return [(tracks[0], resolve_point(db.points(tracks[0].id), when))]
    out = []
    for t in tracks:
        points = db.points(t.id)
        if dated:
            p = as_of_points(points, when)
        else:
            try:
                p = resolve_point(points, when)
            except ValueError:
                p = None
        if p is not None:
            out.append((t, p))
    if not out:
        raise LookupError(f"{label}: no point {when!r} here"
                          + (" — nothing that old" if dated else ""))
    return out


def _served_points(db: SiteDB, repo: str) -> dict[int, int]:
    """{track id: point id} of what is served."""
    state = db.repo_state(repo)
    if state is None or state.serving is None:
        return {}
    return {db.point(p).track: p for p in db.state_points(state.serving.id) if db.point(p)}


def _reserve(db: SiteDB, roots: Roots, repo: str, progress=None, *, again: bool = False,
             empty_ok: bool = False) -> SwitchResult:
    """Serve what the timeline chooses now (pins, else newest complete).
    `again`: switch even to what is served — its site layer is rebuilt.
    `empty_ok`: with nothing to choose, serve nothing (a forced delete of the
    last point); otherwise nothing chosen leaves what is served alone, so a
    store that lost files never unserves a repository."""
    from mmw.site.timeline import choose, ensure_state
    state = db.repo_state(repo)
    store = roots.store(roots.of(state))
    chosen, warnings = choose(db, store, repo)
    if not chosen and not (empty_ok and state.serving is not None):
        return SwitchResult(0, 0, warnings=tuple(warnings))
    vid, more = ensure_state(db, store, repo, chosen)
    if state.serving is not None and state.serving.id == vid and not again:
        result = SwitchResult(0, 0)
    else:
        result = switch_in(db, roots, repo, vid, held=state.held,
                           progress=(lambda rel, _kind: progress(rel)) if progress else None)
    db.drop_unused_states(repo)
    return SwitchResult(result.placed, result.removed, result.held,
                        (*warnings, *more, *result.warnings))


@dataclass(frozen=True)
class ServePreview:
    label: str
    current: list[str]          # "suite date · digest" of what each suite serves
    target: list[str]           # the same, for what it would serve
    relinks: int


def _point_line(db: SiteDB, track_id: int, point_id: int) -> str:
    from mmw.site.versionref import day, short
    p = db.point(point_id)
    return f"{suite_of(db.track(track_id))} {day(p.date)} · {short(p.ident)}"


def serve_preview(home: Path, ref: str, when: str, track: str | None = None) -> ServePreview:
    """What `serve` would do, for its confirmation."""
    from mmw.site.timeline import state_files
    with SiteDB(home) as db:
        repo = resolve_repo(db, ref)
        state = db.repo_state(repo)
        now = _served_points(db, repo)
        then = dict(now)
        for t, p in _targets(db, repo, when, track, as_of=True):
            then[t.id] = p.id
        before = db.served_files(state.serving.id, db.repo_overlay(repo)) if state.serving else {}
        after, _w = state_files(db, then.values())
        changed = sum(1 for p, c in after.items() if before.get(p) != c) + len(set(before) - set(after))
        return ServePreview(state.label, [_point_line(db, t, p) for t, p in sorted(now.items())],
                            [_point_line(db, t, p) for t, p in sorted(then.items())], changed)


def _roots(home: Path) -> Roots:
    return Roots(load_site(home))


def _render(roots: Roots, db: SiteDB | None = None) -> None:
    """After what is served changed: dated trees made to match (with the
    database at hand), then the derived pages."""
    if db is not None:
        from mmw.site.dated import refresh
        refresh(db, roots)
    for root in roots.all:
        if disk_io.ops.is_dir(root.path):
            render_derived(str(root.path))


@contextmanager
def _session(home: Path):
    """Lock the site, open its database, and finish any switch a killed run
    left halfway — before anything else looks at what is served."""
    roots = _roots(home)
    with SiteLock(home), SiteDB(home) as db:
        migrate(db, roots)
        resume_switches(db, roots)
        yield db, roots


def serve(home: Path, ref: str, when: str, track: str | None = None, progress=None) -> SwitchResult:
    """Pin a repository's suite (`track`; None: every suite) to the point
    `when` names — a date serves each suite as it was then — and serve it.
    A pinned suite stays there through later bundles until `forward`.
    `progress(path)` for each file placed or removed."""
    from mmw.site.timeline import is_complete
    with _session(home) as (db, roots):
        repo = resolve_repo(db, ref)
        state = db.repo_state(repo)
        roots.check_mounted(roots.of(state))
        store = roots.store(roots.of(state))
        targets = _targets(db, repo, when, track, as_of=True)
        for t, p in targets:
            missing = is_complete(db, store, p.id)
            if missing:
                raise ValueError(f"{_point_line(db, t.id, p.id)} isn't complete here ({missing} file(s) "
                                 "missing) — bring them with a full bundle first")
        for t, p in targets:
            db.set_pin(t.id, p.id)
        result = _reserve(db, roots, repo, progress, again=True)
        _render(roots, db)
        return result


def _pinned(db: SiteDB, repo: str) -> bool:
    return any(t.pinned is not None for t in db.tracks(repo))


def forward(home: Path, ref: str | None, progress=None) -> list[str]:
    """Clear a repository's hold and pins — one, or (None) every held or
    pinned one — and serve each suite's newest. The repositories moved.
    `progress(event, **data)`: a run page's — each switch, each file placed."""
    moved = []
    with _session(home) as (db, roots):
        repos = ([resolve_repo(db, ref)] if ref is not None
                 else [s.repo for s in db.repo_states() if s.held or _pinned(db, s.repo)])
        for repo in repos:
            roots.check_mounted(roots.of(db.repo_state(repo)))
            for t in db.tracks(repo):
                if t.pinned is not None:
                    db.set_pin(t.id, None)
            db.set_held(repo, False)
            if progress:
                progress("switch", repo=repo)
            result = _reserve(db, roots, repo, _files_progress(progress))
            if progress:
                progress("finished", repo=repo)
            if result.held:
                continue                        # still held: its layer cannot be built yet
            moved.append(repo)
        if moved:
            if progress:
                progress("phase", text="dated trees and pages")
            _render(roots, db)
    return moved


def relayer(home: Path, ref: str, progress=None) -> SwitchResult:
    """Serve the repository's current version again with its site layer as it
    is now — after local packages or the signing choice changed."""
    with _session(home) as (db, roots):
        repo = resolve_repo(db, ref)
        state = db.repo_state(repo)
        if state is None or state.serving is None:
            raise ValueError(f"{ref} serves nothing yet — apply a bundle first")
        roots.check_mounted(roots.of(state))
        held = state.held
        # Held by its layer (not by a person): once the layer builds, go on to
        # the newest version, like any fast-forward.
        layer_held = bool(db.get_meta(f"hold:{repo}"))
        target = state.serving.id
        if layer_held:
            from mmw.site.timeline import choose, ensure_state
            store = roots.store(roots.of(state))
            chosen = choose(db, store, repo)[0]
            if chosen:                       # nothing to choose: keep what is served
                target = ensure_state(db, store, repo, chosen)[0]
        # Layer-held: once it builds, serving the timeline's choice is a fast-forward
        # (the hold clears); otherwise a re-layer keeps the repository's own hold.
        result = switch_in(db, roots, repo, target, held=False if layer_held else held,
                           progress=(lambda rel, _kind: progress(rel)) if progress else None)
        _render(roots, db)
        return result


def status_reason(home: Path, ref: str) -> str | None:
    """Why a repository is held by its site layer, if it is."""
    with _session(home) as (db, _roots):
        return db.get_meta(f"hold:{resolve_repo(db, ref)}") or None


def hold(home: Path, ref: str) -> str:
    """Keep a repository where it is: the next bundle stores its new version
    without serving it."""
    with _session(home) as (db, _roots_):
        repo = resolve_repo(db, ref)
        db.set_held(repo, True)
        return db.repo_state(repo).label


def label_point(home: Path, ref: str, when: str, name: str | None, track: str | None = None) -> list[str]:
    """Name the point `when` names in each suite (or `track`) — None clears
    its name. A suite's name is on one point: another that had it loses it.
    A named point is kept whatever the retention. What changed, in words."""
    from mmw.site.versionref import check_label, day
    name = check_label(name) if name is not None else None
    with _session(home) as (db, _roots_):
        repo = resolve_repo(db, ref)
        label = db.repo_state(repo).label
        out = []
        for t, p in _targets(db, repo, when, track, as_of=False):
            what = f"{label} {suite_of(t)} {day(p.date)}"
            if name is None:
                out.append(f"{what}: name {p.snap_label} cleared" if p.snap_label else f"{what}: had no name")
                db.set_snap_label(p.id, None)
                continue
            moved = [q for q in db.points(t.id, stubs=True) if q.id != p.id and q.snap_label == name]
            for q in moved:
                db.set_snap_label(q.id, None)
            db.set_snap_label(p.id, name)
            out.append(f"{what}: labelled {name}" + (f" (moved from {day(moved[0].date)})" if moved else "")
                       + " — kept whatever the retention")
        return out


# ── space ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PointSpace:
    suite: str
    date: str
    ident: str
    snap_label: str | None
    total: int              # bytes its files take
    frees: int              # bytes deleting it would free (held by nothing else)
    serving: bool


@dataclass
class SpaceReport:
    repos: dict[str, list[PointSpace]] = field(default_factory=dict)     # by suite, newest first
    labels: dict[str, str] = field(default_factory=dict)
    used: int = 0               # the primary root's
    free: int = 0
    history_bytes: int = 0      # what deleting every unserved point would free
    roots: dict[str, tuple[int, int]] = field(default_factory=dict)   # name → (used, free)
    placed: dict[str, str] = field(default_factory=dict)              # repository → root name


def space(home: Path) -> SpaceReport:
    _ensure_migrated(home)
    roots = _roots(home)
    rep = SpaceReport()
    with SiteDB(home) as db:
        for st in db.repo_states():
            rep.placed[st.repo] = roots.of(st).name
            rows = []
            served = set(_served_points(db, st.repo).values())
            for t in db.tracks(st.repo):
                for pt in db.points(t.id):
                    total, frees = db.point_sizes(pt.id)
                    serving = pt.id in served
                    rows.append(PointSpace(suite_of(t), pt.date, pt.ident, pt.snap_label, total, frees, serving))
                    if not serving:
                        rep.history_bytes += frees
            rep.repos[st.repo], rep.labels[st.repo] = rows, st.label
    for r in roots.all:
        if disk_io.ops.is_dir(r.path):
            from mmw.site.space import real_free
            usage = disk_io.ops.disk_usage(r.path)
            rep.roots[r.name] = (usage.used, real_free(r.path))      # on WSL, the Windows drive's too
    rep.used, rep.free = rep.roots.get(roots.primary.name, (0, 0))
    return rep


def _files_progress(progress):
    """A `progress(event, **data)` (a run page's) as the per-file callback
    switches and the store take: each file one tick, named."""
    return (lambda rel, *_kind: progress("receive", bytes=1, name=rel)) if progress else None


def _gc(db: SiteDB, roots: Roots, progress=None) -> int:
    # Only blobs nothing references go — never a held point's — so what is
    # known complete stays complete: no point needs checking again.
    if progress:
        progress("phase", text="freeing space: removing files nothing keeps")
    freed = 0
    tick = _files_progress(progress)
    for r in roots.all:
        if disk_io.ops.is_dir(Path(r.path) / ".mmw" / "store"):
            freed += roots.store(r).gc(roots.referenced(db, r), progress=tick)[1]
    return freed


def _protected(db: SiteDB, repo: str, track: TrackInfo, point: PointInfo) -> tuple[str, str] | None:
    """Why a point can't be deleted — (kind, reason) — or None. Kind
    "served" may be overridden (`force`); the others are what a pin, a
    rollback or a running switch depends on."""
    from mmw.site.versionref import day
    what, label = f"{suite_of(track)} {day(point.date)}", db.repo_state(repo).label
    for _r, f, t, _p in db.pending_switches():
        if any(point.id in db.state_points(v) for v in (f, t) if v is not None):
            return "pending", f"{what} is what an unfinished switch is going to or from"
    if track.pinned == point.id:
        return "pinned", f"{what} is pinned — mmw site forward '{label}' first"
    if track.previous == point.id:
        return "previous", f"{what} is what {suite_of(track)} served before — kept for rolling back"
    if point.id in _served_points(db, repo).values():
        nxt = next((p for p in db.points(track.id) if p.id != point.id), None)
        return "served", (f"{what} is being served — pass --yes to delete it anyway; {suite_of(track)} then "
                          f"serves {day(nxt.date) if nxt else 'nothing'}")
    if point.snap_label:
        return "labelled", (f"{what} is labelled {point.snap_label} — pass --yes to delete it anyway; "
                            "its name goes with it")
    return None


def delete_point(home: Path, ref: str, when: str, track: str | None = None, force: bool = False,
                 progress=None) -> int:
    """Delete a kept point: its file list stays (a stub), its files go
    unless something else holds them. Bytes freed."""
    with _session(home) as (db, roots):
        repo = resolve_repo(db, ref)
        found = _targets(db, repo, when, track, as_of=False)
        if len(found) > 1:
            raise ValueError(f"{when!r} names a point in {len(found)} suites "
                             f"({', '.join(suite_of(t) for t, _p in found)}) — say which: --track SUITE")
        t, p = found[0]
        why = _protected(db, repo, t, p)
        if why and not (why[0] in ("served", "labelled") and force):
            raise ValueError(why[1])
        db.stub_point(p.id)
        if p.snap_label:
            db.set_snap_label(p.id, None)
        if why and why[0] == "served":
            # Serve nothing only when nothing of it is kept any more: kept points
            # whose files are missing here keep what is served, never unserve it.
            last = not any(db.points(t.id) for t in db.tracks(repo))
            if progress:
                progress("phase", text="serving what is kept instead")
            _reserve(db, roots, repo, _files_progress(progress), empty_ok=last)
        if progress:
            progress("phase", text="dated trees and pages")
        _render(roots, db)                    # its dated tree goes before its blobs can
        db.drop_unused_states(repo)
        return _gc(db, roots, progress)


@dataclass(frozen=True)
class CleanItem:
    repo: str
    label: str
    suite: str
    date: str          # YYYY-MM-DD
    point: int
    frees: int         # what it alone frees: bytes no other held point shares


def keep_of(settings, track: TrackInfo) -> "int | str":
    """The history a suite keeps: its own setting, else the site's (a stored
    value that doesn't read keeps everything — never less)."""
    from mmw.site.home import parse_keep
    try:
        return parse_keep(track.keep) if track.keep else parse_keep(settings.keep)
    except ValueError:
        return "all"


def _past(points, keep, today: str) -> list:
    """The points (newest first) past `keep`: the newest N, none for "all",
    or those dated before the age."""
    if keep == "all":
        return []
    if isinstance(keep, int):
        return list(points[keep:])
    from datetime import datetime, timedelta, timezone
    now = datetime.strptime(today, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    cutoff = (now - timedelta(days=int(keep[:-1]))).strftime("%Y%m%dT%H%M%SZ")
    return [p for p in points[1:] if p.date < cutoff]         # a suite's newest always stays


def clean_plan(db: SiteDB, settings, today: str | None = None) -> list[CleanItem]:
    """What Clean up would delete: per suite, the points past its setting
    that nothing protects (served, served before, pinned, mid-switch)."""
    from mmw.core.publisher.manifest import now_stamp
    from mmw.site.versionref import day
    today = today or now_stamp()
    out = []
    for st in db.repo_states():
        for t in db.tracks(st.repo):
            for pt in _past(db.points(t.id), keep_of(settings, t), today):
                if _protected(db, st.repo, t, pt) is None:
                    out.append(CleanItem(st.repo, st.label, suite_of(t), day(pt.date), pt.id,
                                         db.point_sizes(pt.id)[1]))
    return out


def clean_plan_here(home: Path, today: str | None = None) -> list[CleanItem]:
    _ensure_migrated(home)
    with SiteDB(home) as db:
        return clean_plan(db, load_site(home), today)


def _clean(db: SiteDB, roots: Roots, settings, only: set[int] | None = None,
           progress=None) -> tuple[list[CleanItem], int]:
    items = clean_plan(db, settings)
    if only is not None:
        items = [i for i in items if i.point in only]        # what was confirmed, if it still applies
    for i in items:
        if progress:
            progress("phase", text=f"deleting {i.label} {i.suite} {i.date}")
        db.stub_point(i.point)
    from mmw.site.dated import refresh
    if progress and items:
        progress("phase", text="dated trees: taking the deleted days' away")
    refresh(db, roots)                        # a cleaned point's dated tree goes before its blobs can
    return items, (_gc(db, roots, progress) if items else 0)


def clean(home: Path, points: "set[int] | None" = None, progress=None) -> int:
    """Clean up: delete what `clean_plan` names — only `points` (what was
    shown and confirmed) when given, and only those it still names. Bytes
    freed. `progress(event, **data)`: a run page's."""
    with _session(home) as (db, roots):
        return _clean(db, roots, load_site(home), points, progress)[1]


def apply_retention(db: SiteDB, roots: Roots, settings) -> list[tuple[str, str, str]]:
    """After an apply, with `auto_clean`: Clean up. The (repository, suite,
    date) deleted."""
    items, _freed = _clean(db, roots, settings)
    return [(i.repo, i.suite, i.date) for i in items]


def keep_costs(db: SiteDB, settings, presets=(1, 3, 10, "all")) -> dict:
    """Per preset of the site's `keep`, the bytes Clean up would free — what
    choosing it costs or saves against keeping everything."""
    held: dict[int, set[str]] = {}
    states: list[tuple[set[int], set[str]]] = []       # each stored state: its points, its files
    base = set(db.network_files().values())
    for st in db.repo_states():
        for t in db.tracks(st.repo):
            for p in db.points(t.id):
                held[p.id] = set(db.point_files(p.id).values())
        for v in db.versions(st.repo):
            states.append((set(db.state_points(v.id)), set(db.version_files(v.id).values())))
        if st.serving is not None:                      # what is served, with its site layer: always kept
            base |= set(db.served_files(st.serving.id, db.repo_overlay(st.repo)).values())
    everything = set().union(base, *held.values(), *(f for _p, f in states))
    sizes = db.blob_sizes(everything)
    out = {}
    for preset in presets:
        trial = settings.model_copy(update={"keep": preset})
        gone = {i.point for i in clean_plan(db, trial)}
        # A state holding a point that goes goes with it (as Clean up drops it); the rest keep their files.
        kept = set().union(base, *(cs for pid, cs in held.items() if pid not in gone),
                           *(f for pts, f in states if not pts & gone))
        out[preset] = sum(sizes.get(c, 0) for c in everything - kept)
    return out


def keep_costs_here(home: Path) -> dict:
    _ensure_migrated(home)
    settings = load_site(home)
    from mmw.site.home import parse_keep
    try:
        current = parse_keep(settings.keep)
    except ValueError:
        current = None
    presets = tuple(dict.fromkeys([1, 3, 10, "all", *([current] if current is not None else [])]))
    with SiteDB(home) as db:
        return keep_costs(db, settings, presets)


def set_track_keep(home: Path, ref: str, track: str, keep) -> None:
    """A suite's own history setting (None: the site's)."""
    from mmw.site.home import parse_keep
    value = None if keep is None else str(parse_keep(keep))
    with _session(home) as (db, _roots):
        repo = resolve_repo(db, ref)
        db.set_track_keep(_track_named(db, repo, track).id, value)


# ── status, history, verify ──────────────────────────────────────────────────

def _ensure_migrated(home: Path) -> None:
    """A site from before timelines gets them on its first look, not only
    on its first change — unless another run holds the lock (it migrates)."""
    with SiteDB(home) as db:
        if db.get_meta("timeline"):
            return
    from mmw.site.lock import LockHeld
    try:
        with _session(home):
            pass
    except LockHeld:
        pass


def converted_notice(home: Path) -> str | None:
    """Take the pending conversion notice once, under the site's lock."""
    from mmw.site.lock import LockHeld
    import json
    with SiteDB(home) as db:
        if not db.get_meta("timeline") or not db.get_meta("timeline_conversion_notice"):
            return None
    try:
        with SiteLock(home), SiteDB(home) as db:
            raw = db.get_meta("timeline_conversion_notice")
            if not raw:
                return None
            paths = json.loads(raw)
            db.set_meta("timeline_conversion_notice", "")
    except LockHeld:
        return None
    return ("converted to timelines (one-way) — the old database is kept in "
            + ", ".join(paths) + "; remove it once you're satisfied")


def conversion_backups(home: Path) -> list[str]:
    """Control-state backups an operator may remove once satisfied."""
    path = Path(home).with_name("site.before-timelines")
    return [str(path)] if disk_io.ops.is_dir(path) else []


def status(home: Path):
    """Every repository's state, by label."""
    _ensure_migrated(home)
    with SiteDB(home) as db:
        return db.repo_states()


def unverified(home: Path, repo: str) -> list[str]:
    """"suite date" of each of the repository's points adopted without a
    signature check — said until they are checked."""
    from mmw.site.adopt import unverified_key
    with SiteDB(home) as db:
        marked = set(db.metas("unverified:"))
        out = []
        for t in db.tracks(repo):
            for p in db.points(t.id):
                if unverified_key(p) in marked:
                    out.append(f"{suite_of(t)} {p.date[:4]}-{p.date[4:6]}-{p.date[6:8]}")
        return out


def narrowing(home: Path, ref: str) -> list[str]:
    """`combine.narrowing` of a repository: what its suites no longer serve."""
    from mmw.site.combine import narrowing as lines
    with SiteDB(home) as db:
        return lines(db, resolve_repo(db, ref))


def deleted_stamps(home: Path, ref: str) -> list[str]:
    """When the bundles that brought a repository's deleted versions were made."""
    with SiteDB(home) as db:
        return db.deleted_stamps(resolve_repo(db, ref))


def history(home: Path, ref: str) -> list[tuple[TrackInfo, list[tuple[PointInfo, int, int, str]]]]:
    """A repository's suites, each with its points newest first: (point,
    total bytes, bytes deleting it frees, mark) — mark is serving, pinned,
    previous, incomplete, stub, or ""."""
    from mmw.site.timeline import is_complete
    _ensure_migrated(home)
    roots = _roots(home)
    with SiteDB(home) as db:
        repo = resolve_repo(db, ref)
        store = roots.store(roots.of(db.repo_state(repo)))
        served = set(_served_points(db, repo).values())
        out = []
        for t in db.tracks(repo):
            rows = []
            for pt in db.points(t.id, stubs=True):
                total, frees = db.point_sizes(pt.id)
                # A deleted point is a stub first: it can't be rolled back to.
                mark = ("serving" if pt.id in served else "stub" if not pt.held
                        else "pinned" if t.pinned == pt.id else "previous" if t.previous == pt.id
                        else "incomplete" if is_complete(db, store, pt.id) else "")
                rows.append((pt, total, 0 if not pt.held else frees, mark))
            if rows:
                out.append((t, rows))
        return out


def diff(home: Path, ref: str, a: str, b: str | None = None, track: str | None = None) -> list:
    """Per suite (`track`: just that one) with a point `a` names: what changed
    from it to the point `b` names — by default the next kept point after it."""
    from mmw.core.point_diff import SuiteDiff, diff as compare
    from mmw.site.versionref import resolve_point
    roots = _roots(home)
    with SiteDB(home) as db:
        repo = resolve_repo(db, ref)
        state = db.repo_state(repo)
        packages = _packages_reader(db, roots.store(roots.of(state)))

        out, last, refused = [], [], None
        for t in [_track_named(db, repo, track)] if track else db.tracks(repo):
            kept = sorted(db.points(t.id), key=lambda p: (p.date, p.stamp, p.ident))
            try:
                old = resolve_point(kept, a)
                new = resolve_point(kept, b) if b is not None else next(iter(kept[kept.index(old) + 1:]), None)
            except ValueError as e:          # not in this suite: said only if no suite has it
                refused = refused or e
                continue
            if new is None:
                last.append(suite_of(t))
                continue
            out.append(SuiteDiff(state.label, suite_of(t), old.date, new.date, compare(packages(old), packages(new))))
    if not out:
        if last and b is None:
            raise LookupError(f"{a} is the newest point of {', '.join(last)} kept here — nothing after it to compare")
        raise refused or LookupError(f"{state.label}: no point {a!r} here")
    return out


def package_history(home: Path, ref: str, name: str) -> list:
    """Per suite, oldest first: each point kept here where `name`'s version
    changed — the version it arrived with, each new one, and the point it
    left (version None). A point whose files aren't all here is skipped."""
    from mmw.core.point_diff import PackageEvent, package_events
    from mmw.site.timeline import is_complete
    roots = _roots(home)
    with SiteDB(home) as db:
        repo = resolve_repo(db, ref)
        state = db.repo_state(repo)
        store = roots.store(roots.of(state))
        packages = _packages_reader(db, store)
        out = []
        for t in db.tracks(repo):
            kept = [p for p in sorted(db.points(t.id), key=lambda p: (p.date, p.stamp, p.id))
                    if not is_complete(db, store, p.id)]
            out += [PackageEvent(state.label, suite_of(t), p.date, v, p.id)
                    for p, v in package_events(name, kept, packages)]
    if not out:
        raise LookupError(f"{name} isn't in any kept point")
    return out


def _packages_reader(db: SiteDB, store: Store):
    """point → its (package, arch) → version, read from `store`."""
    from mmw.core.point_diff import packages_of

    def packages(point):
        files = db.point_files(point.id)

        def read(path):
            checksum = files.get(path)
            try:
                return disk_io.ops.read_bytes(store.path(checksum)) if checksum else None
            except OSError:
                return None
        return packages_of(files, read)
    return packages


def point_rows(home: Path, ref: str) -> dict[int, dict]:
    """Point id → what the far side's history says beside it:
    - "changes": [added, removed, upgraded, downgraded] since the kept point
      before it in its suite — absent for the oldest, and for a pair either
      of which isn't complete here. Worked out once per pair, then kept.
    - "covers": its arches and components ("amd64 arm64 · main"), only
      where its suite's points don't all cover the same.
    - "components": what a sources.list line names.
    - "dated": `https://host/path/@YYYY-MM-DD` (http without a certificate)
      when that day's dated tree is built."""
    import json as _json
    import sqlite3

    from mmw.core.point_diff import diff as compare
    from mmw.site.combine import _selection_of
    from mmw.site.dated import _day
    from mmw.site.timeline import is_complete
    roots = _roots(home)
    with SiteDB(home) as db:
        repo = resolve_repo(db, ref)
        root = roots.of(db.repo_state(repo))
        store = roots.store(root)
        packages = _packages_reader(db, store)
        dirs = set()
        if roots.settings.dated:
            for k, rec in _json.loads(db.get_meta(f"dated:{root.name}") or "{}").items():
                if k.split("|", 1)[0] == repo and isinstance(rec, dict):
                    dirs.update(rec.get("dirs", []))
        bases = sorted(db.repo_state(repo).bases)
        tls = disk_io.ops.exists(Path(roots.primary.path) / ".mmw" / "tls" / "mirror.crt")
        scheme = "https" if tls else "http"
        out: dict[int, dict] = {}
        for t in db.tracks(repo):
            kept = sorted(db.points(t.id), key=lambda p: (p.date, p.stamp, p.id))      # oldest first
            sel = {p.id: _selection_of(db, p.id, t.name) for p in kept} if t.name else {}
            differ = len({(tuple(s["arches"]), tuple(s["components"])) for s in sel.values()}) > 1
            before = None
            for p in kept:
                row: dict = {}
                s = sel.get(p.id)
                if s is not None:
                    comps = s["components"]
                    row["components"] = sorted(comps, key=lambda c: (c != "main", c))
                    if differ:
                        row["covers"] = f"{' '.join(s['arches']) or '—'} · {' '.join(row['components']) or '—'}"
                dated = next((f"{scheme}://{b}/@{_day(p.date)}" for b in bases
                              if f"{b}/@{_day(p.date)}" in dirs), None)
                if dated:
                    row["dated"] = dated
                if before is not None:
                    key = f"changes:{before.id}:{p.id}"
                    said = db.get_meta(key)
                    if said:
                        row["changes"] = _json.loads(said)
                    elif not is_complete(db, store, before.id) and not is_complete(db, store, p.id):
                        try:
                            row["changes"] = list(compare(packages(before), packages(p)).counts)
                        except ValueError:                 # an unreadable index: nothing said
                            pass
                        else:
                            try:
                                db.set_meta(key, _json.dumps(row["changes"]))
                            except sqlite3.OperationalError:   # a busy database: worked out again next time
                                pass
                out[p.id] = row
                before = p
        return out


def _expected(db: SiteDB, roots: Roots) -> dict[str, dict[str, str]]:
    """Root name → {path: checksum} it should serve."""
    out: dict[str, dict[str, str]] = {r.name: {**roots.network_files(db, r), **roots.generated(db, r)}
                                      for r in roots.all}
    for st in db.repo_states():
        if st.serving is not None:            # the version and its site layer
            out[roots.of(st).name].update(db.served_files(st.serving.id, db.repo_overlay(st.repo)))
    return out


def _wrong(root: Path, store: Store, expected: dict[str, str]):
    """(rel, checksum, what is wrong) for every served file not as it should be."""
    import hashlib
    from mmw.core.served_tree import ROOT_PLACEHOLDER, _nginx_root_config
    for rel, checksum in sorted(expected.items()):
        path = root / rel
        if not disk_io.ops.exists(path):
            yield rel, checksum, "missing"
            continue
        if _nginx_root_config(rel) and store.has(checksum):
            want = hashlib.sha256(disk_io.ops.read_bytes(store.path(checksum)).replace(
                ROOT_PLACEHOLDER.encode(), str(root).encode())).hexdigest()
        else:
            want = checksum
        if disk_io.hash_file(path) != want:
            yield rel, checksum, "differs from what the site serves"


def verify_points(home: Path, ref: str | None = None):
    """Check all kept points using their owning root's immutable store."""
    from mmw.core.point_check import PointProblem, check_suite
    from mmw.site.versionref import day
    roots = _roots(home)
    problems = []
    with SiteDB(home) as db:
        selected = resolve_repo(db, ref) if ref is not None else None
        for state in db.repo_states():
            if selected is not None and state.repo != selected:
                continue
            store = roots.store(roots.of(state))
            for track in db.tracks(state.repo):
                if not track.name:
                    continue
                for point in db.points(track.id):
                    files = db.point_files(point.id)
                    def read(path):
                        checksum = files.get(path)
                        try:
                            return disk_io.ops.read_bytes(store.path(checksum)) if checksum else None
                        except OSError:
                            return None
                    found = check_suite(files, track.name, read)
                    if found:
                        problems.append(PointProblem(state.repo, suite_of(track), day(point.date), found))
    return problems


def verify(home: Path) -> list[str]:
    """Every served file present and exactly what the site says it serves.
    The problems found (none: the tree is right)."""
    roots = _roots(home)
    with SiteDB(home) as db:
        expected = _expected(db, roots)
    multi = len(roots.all) > 1
    problems = []
    for r in roots.all:
        for rel, _cs, why in _wrong(Path(r.path), roots.store(r), expected[r.name]):
            problems.append(f"{r.name}: {rel}: {why}" if multi else f"{rel}: {why}")
    return problems


def repair(home: Path) -> int:
    """Put every wrong or missing served file back from the store; how many."""
    from mmw.site.apply import place_served_file
    fixed = 0
    with _session(home) as (db, roots):
        network = set(db.network_files())
        for r in roots.all:
            root, store = Path(r.path), roots.store(r)
            for rel, checksum, _why in list(_wrong(root, store, _expected(db, roots)[r.name])):
                if not store.has(checksum):
                    continue                              # generated nginx: the next apply rewrites it
                if disk_io.ops.exists(root / rel):
                    disk_io.ops.remove(root / rel)        # never write through a damaged link
                place_served_file(store, root, rel, checksum, network=rel in network)
                fixed += 1
    return fixed


# ── moving a repository to another root ─────────────────────────────────────

@dataclass(frozen=True)
class MoveResult:
    copied_bytes: int
    files: int
    nginx_changed: list[str]
    tls_notes: list[str] = field(default_factory=list)
    moved: list[str] = field(default_factory=list)        # labels: the repository, and any sharing its hostname


@dataclass(frozen=True)
class MovePreview:
    """What a move takes, for its confirmation: the repositories (more than
    the one asked for when they share a hostname), their hostnames, and the
    bytes the target root lacks."""
    labels: list[str]
    hosts: list[str]
    copies: int
    repos: list[str] = field(default_factory=list)        # their keys, in the same order

    @property
    def whole_host(self) -> bool:
        return len(self.labels) > 1


# ── a set of roots ────────────────────────────────────────────────────────────
# Everything here goes through the root interface (`mset.LocalRoot`; an ssh
# root answers the same calls remotely) — never into a root's files.

def set_status(mset) -> list[tuple[str, "RepoState"]]:
    """Every repository of every root here: (root name, its state)."""
    out = [(root.info.name, st) for root in mset.roots for st in root.status()]
    return sorted(out, key=lambda x: x[1].label.lower())


def on_repo(mset, ref: str):
    """The root holding repository `ref`, and its key."""
    return mset.root_for_repo(ref)


def set_space(mset) -> dict[str, "SpaceReport"]:
    return {root.info.name: root.space() for root in mset.roots}


def set_verify(mset, repair_too: bool = False) -> dict[str, "list[str] | int"]:
    return {root.info.name: root.verify(repair_too) for root in mset.roots}


def set_keep(mset, keep) -> None:
    """The history every root here keeps (N, "all" or an age) — said, not
    acted on: Clean up applies it."""
    for root in mset.roots:
        root.keep(keep)


def keep_here(home: Path, keep, auto_clean: bool | None = None, dated: bool | None = None) -> None:
    from mmw.site.home import parse_keep, save_settings
    with _session(home) as (db, roots):
        settings = load_site(home)
        update = {}
        if keep is not None:
            update["keep"] = parse_keep(keep)
        if auto_clean is not None:
            update["auto_clean"] = auto_clean
        if dated is not None:
            update["dated"] = dated
        save_settings(home, settings.model_copy(update=update))
        if dated is not None:
            from mmw.site.dated import refresh
            refresh(db, _roots(home))                    # with the settings just saved


def _hosts_of(bases: list[str]) -> set[str]:
    return {b.split("/", 1)[0] for b in bases}


# One root's halves of a move (each under that root's own lock).

def move_out_info(home: Path, key: str) -> dict:
    """What moving `key` out of this root carries: its versions (files with
    sizes), what it serves, and which repositories share its hostname."""
    with _session(home) as (db, roots):
        state = db.repo_state(key)
        hosts = _hosts_of(state.bases)
        store = roots.store(roots.primary)
        versions = []
        for v in sorted(db.versions(key), key=lambda v: v.number):
            files = [(rel, c, disk_io.ops.stat(store.path(c)).st_size) for rel, c in db.version_files(v.id).items()]
            versions.append({"number": v.number, "label": v.label, "digest": v.digest, "stamp": v.stamp,
                             "snap_label": v.snap_label, "files": files})
        # Its site layer travels too: the policy, and the debs (as blobs to
        # copy, then written into the new root's layer folder).
        layer = db.layer(key)
        from mmw.site.layers import site_debs
        debs = site_debs(home, key)
        for ds in debs.values():
            for d in ds:
                if not store.has(d.sha256):
                    with disk_io.ops.open_rb(d.path) as fh:
                        store.add_stream(fh, expect=d.sha256)
        served = set(_served_points(db, key).values())
        tracks = []
        for t in db.tracks(key):
            pts = db.points(t.id, stubs=True)
            byid = {p.id: p.ident for p in pts}
            rows = []
            for pt in pts:
                files = db.point_files(pt.id)
                sizes = db.blob_sizes(files.values())
                rows.append({"ident": pt.ident, "date": pt.date, "stamp": pt.stamp, "snap_label": pt.snap_label,
                             "held": pt.held, "serving": pt.id in served,
                             "files": [(rel, c, sizes.get(c, 0)) for rel, c in files.items()]})
            tracks.append({"name": t.name, "suite": t.suite, "codename": t.codename, "points": rows,
                           "pinned": byid.get(t.pinned), "previous": byid.get(t.previous),
                           "networks": db.track_networks(t.id), "gone": t.gone})
        return {"label": state.label, "bases": state.bases, "held": state.held, "tracks": tracks,
                "layer": ({"signing": layer.signing, "key": layer.key, "contents": layer.contents,
                           "drop_valid_until": layer.drop_valid_until} if layer else None),
                "layer_files": [(f"{s}/{c}/{d.path.name}", d.sha256, d.size)
                                for (s, c), ds in debs.items() for d in ds],
                # What it serves here, layer and all — what `tidy` clears after the move.
                "served_paths": sorted(db.served_files(state.serving.id, db.repo_overlay(key)))
                if state.serving else [],
                "serving": state.serving.number if state.serving else None, "versions": versions,
                "hosts": sorted(hosts),
                "sharing": [s.label for s in db.repo_states() if s.repo != key and hosts & _hosts_of(s.bases)],
                "sharing_keys": [s.repo for s in db.repo_states() if s.repo != key and hosts & _hosts_of(s.bases)]}


def move_in(home: Path, key: str, info: dict) -> bool:
    """Record a moved repository's versions here (blobs already in the
    store), place its served version; whether nginx changed."""
    from mmw.core.publisher import replay_kind
    from mmw.site.apply import place_served_file, write_root_nginx
    from mmw.site.switch import PHASES, _base
    from mmw.core.served_tree import is_safe_path
    for files in [*(v["files"] for v in info["versions"]),
                  *(pt["files"] for t in info.get("tracks", []) for pt in t["points"])]:
        for rel, c, _size in files:                  # another root's word: checked, never trusted
            if not is_safe_path(rel):
                raise ValueError(f"{rel!r} is outside the root — refusing the move")
    with _session(home) as (db, roots):
        store, path = roots.store(roots.primary), Path(roots.primary.path)
        served_id = None
        for v in info["versions"]:
            vid = db.add_version(key, info["label"], v["number"], v["digest"], v["stamp"], v["snap_label"],
                                 info["bases"], [tuple(f) for f in v["files"]])
            if v["number"] == info["serving"]:
                served_id, served = vid, {rel: c for rel, c, _s in v["files"]}
        serving_points = []
        for t in info.get("tracks", []):
            from mmw.site.tracks import PointDraft
            tid = db.ensure_track(key, t["name"], {k: v for k, v in (("Suite", t.get("suite")),
                                                                   ("Codename", t.get("codename"))) if v})
            ids = {}
            for pt in t["points"]:
                files = {rel: c for rel, c, _s in pt["files"]}
                pid, _st = db.import_point(tid, PointDraft(t["name"], pt["ident"], pt["date"], {}, files),
                                           {c: size for _r, c, size in pt["files"]}, pt["stamp"], pt["snap_label"])
                if not pt["held"]:
                    db.stub_point(pid)
                ids[pt["ident"]] = pid
                if pt.get("serving"):
                    serving_points.append(pid)
            db.set_pin(tid, ids.get(t.get("pinned")))
            db.set_previous(tid, ids.get(t.get("previous")))
            if "networks" in t:
                db.restore_track_presence(tid, t["networks"], t["gone"])
        if served_id is not None and serving_points:
            db.set_state_points(served_id, serving_points)
        if served_id is not None:                     # placed, in package → index → release order
            for kind in PHASES:
                for rel in sorted(served):
                    if replay_kind(rel, _base(rel, info["bases"])) == kind:
                        store.place(served[rel], path / rel)
            db.set_serving(key, served_id, info["held"])
        for rel, checksum in roots.network_files(db, roots.primary).items():   # keys, the nginx image
            if not disk_io.ops.exists(path / rel) and store.has(checksum):
                place_served_file(store, path, rel, checksum, network=True)
        changed = bool(roots.multi and db.get_meta("settings") and write_root_nginx(db, roots))
        _render(roots, db)
        if info.get("layer"):
            from mmw.site.layers import site_layer_dir
            db.set_layer(key, **info["layer"])
            folder = site_layer_dir(home, key)
            for rel, c, _size in info.get("layer_files", []):
                if not is_safe_path(rel):
                    raise ValueError(f"{rel!r} is not a layer path — refusing the move")
                dest = folder / rel
                disk_io.ops.mkdir(dest.parent, parents=True, exist_ok=True)
                disk_io.ops.copy_file(store.path(c), dest)
    if info.get("layer") and served_id is not None:
        relayer(home, key)               # serve it with its layer here (held, with why, if it cannot be)
    return changed


def move_out_finish(home: Path, key: str, to: str, info: dict) -> bool:
    """The repository lives in `to` now: forget it here, keeping its served
    files until `tidy`; whether nginx changed."""
    import json
    from mmw.site.apply import write_root_nginx
    served = next((v for v in info["versions"] if v["number"] == info["serving"]), None)
    with _session(home) as (db, roots):
        paths = info.get("served_paths") or (sorted(rel for rel, _c, _s in served["files"]) if served else [])
        db.set_meta(f"moved:{key}", json.dumps({"to": to, "label": info["label"], "files": paths}))
        db.drop_repo(key)
        from mmw.site.layers import site_layer_dir
        folder = site_layer_dir(home, key)
        if disk_io.ops.exists(folder):
            disk_io.ops.rmtree(folder)       # the layer lives in the new root now
        from mmw.site.dated import refresh
        refresh(db, roots)                   # its dated trees went with it
        return bool(roots.multi and db.get_meta("settings") and write_root_nginx(db, roots))


def set_moving_in(home: Path, key: str, src: str) -> None:
    with SiteLock(home), SiteDB(home) as db:
        db.set_meta(f"moving_in:{key}", src)


def tidy_here(home: Path, progress=None) -> list[str]:
    """Clear moved repositories' old served files here; the labels tidied.
    `progress(path)` for each file removed."""
    import json
    from mmw.core.served_tree import _prune_empty_parents
    done = []
    with _session(home) as (db, roots):
        path = Path(roots.primary.path)
        here = {p for s in db.repo_states() if s.serving is not None
                for p in db.served_files(s.serving.id, db.repo_overlay(s.repo))}
        for k, raw in db.metas("moved:").items():
            rec = json.loads(raw or "{}")
            if not rec:
                continue
            for rel in rec.get("files", []):
                if rel not in here and disk_io.ops.remove(path / rel) is not None:
                    _prune_empty_parents(str(path), str((path / rel).parent))
                    if progress:
                        progress(rel)
            db.set_meta(k, "{}")
            done.append(rec.get("label", k.split(":", 1)[1]))
        roots.store(roots.primary).gc(roots.referenced(db, roots.primary))
    return done


def _move_plan(mset, ref: str, to_root: str):
    """The repositories that move together — `ref` and every one in its root
    sharing a hostname with it, directly or through another, since a
    hostname is served from one root — with what each carries."""
    src, key = mset.root_for_repo(ref)
    dst = mset.root_named(to_root)
    if src.info.name == dst.info.name:
        raise ValueError(f"{ref} is already in {dst.info.name}")
    infos: dict[str, dict] = {}
    todo = [key]
    while todo:
        k = todo.pop()
        if k in infos:
            continue
        info = src.move_out_info(k)
        if info["sharing"] and "sharing_keys" not in info:      # an older mmw there can't say which
            raise ValueError(f"{info['label']} shares {', '.join(info['hosts'])} with "
                             f"{', '.join(info['sharing'])} — update mmw on {src.info.name} to move them together")
        infos[k] = info
        todo += info.get("sharing_keys", [])
    group = sorted(infos, key=lambda k: infos[k]["label"].lower())
    sizes = {c: size for info in infos.values() for v in info["versions"] for _rel, c, size in v["files"]}
    sizes.update({c: size for info in infos.values() for t in info.get("tracks", []) for pt in t["points"]
                  if pt["held"] for _rel, c, size in pt["files"]})
    sizes.update({c: size for info in infos.values() for _rel, c, size in info.get("layer_files", [])})
    lacking = set(sizes) - dst.has(set(sizes))
    return src, dst, group, infos, sizes, lacking


def move_preview(mset, ref: str, to_root: str) -> MovePreview:
    _src, _dst, group, infos, sizes, lacking = _move_plan(mset, ref, to_root)
    return MovePreview([infos[k]["label"] for k in group],
                       sorted({h for info in infos.values() for h in info["hosts"]}),
                       sum(sizes[c] for c in lacking), list(group))


def move_size(mset, ref: str, to_root: str) -> int:
    """Bytes moving `ref` (with any repository sharing its hostname) to
    `to_root` copies: what that root lacks."""
    return move_preview(mset, ref, to_root).copies


def move_repo(mset, ref: str, to_root: str, progress=None, stop_event=None) -> MoveResult:
    """Move a repository, every kept version, to another root of the set —
    and with it every repository sharing a hostname with it: a hostname is
    served from one root, so the whole host moves.

    Everything is copied into the target's store first (`progress("repo",
    repo=, label=)`, then `("copy", bytes=)` per file); a stop there switches
    nothing, and moving again picks up from what was copied. Then each
    repository switches (`"switch"`): its served version placed there before
    the old root lets go, so a move cut short runs again; then `"done"`. The
    old root keeps serving its copy (the nginx running there still routes the
    hostname to it) until `tidy_set`, once nginx has been recreated in both
    roots."""
    from mmw.core.format import fmt_size
    say = progress or (lambda *a, **k: None)
    src, dst, group, infos, sizes, lacking = _move_plan(mset, ref, to_root)
    labels = ", ".join(infos[k]["label"] for k in group)
    need, free = sum(sizes[c] for c in lacking), dst.free_bytes()
    if need > free:
        raise ValueError(f"moving {labels} needs {fmt_size(need)} in {dst.info.name}, which has "
                         f"{fmt_size(free)} free — nothing was copied")
    # Until the old root lets go, a copy is not the repository's home: a
    # move cut short runs again from the old root.
    left = set(lacking)
    for k in group:
        dst.set_moving_in(k, src.info.name)
        say("repo", repo=k, label=infos[k]["label"])
        wanted = {c for v in infos[k]["versions"] for _rel, c, _size in v["files"]}
        wanted |= {c for t in infos[k].get("tracks", []) for pt in t["points"] if pt["held"]
                   for _rel, c, _size in pt["files"]}
        wanted |= {c for _rel, c, _size in infos[k].get("layer_files", [])}
        for c in sorted(wanted & left):
            if stop_event is not None and stop_event.is_set():
                raise ValueError(f"stopped — nothing switched; what was copied stays, and moving {labels} "
                                 "again picks up from there")
            with src.export_blob(c) as fh:
                dst.import_blob(fh, c)
            left.discard(c)
            say("copy", bytes=sizes[c])
    if stop_event is not None and stop_event.is_set():
        raise ValueError(f"stopped — nothing switched; moving {labels} again picks up from there")
    changed: list[str] = []
    for k in group:
        say("switch", repo=k)
        if dst.move_in(k, infos[k]) and dst.info.name not in changed:
            changed.append(dst.info.name)
        if src.move_out_finish(k, dst.info.name, infos[k]) and src.info.name not in changed:
            changed.insert(0, src.info.name)
        dst.set_moving_in(k, "")
    hosts = {h for info in infos.values() for h in info["hosts"]}
    for root in mset.roots:
        root.set_identity(hosts={**root.hosts_map(), **{h: dst.info.name for h in hosts}})
    say("done")
    return MoveResult(need, len(lacking), changed, dst.tls_notes() + src.tls_notes(),
                      [infos[k]["label"] for k in group])


def tidy_set(mset, progress=None) -> list[str]:
    """Clear moved repositories' old copies (once nginx has been recreated);
    the labels tidied. `progress(path)` per file removed, where a root can
    say (an ssh root says only when it's done)."""
    return [label for root in mset.roots for label in root.tidy(progress=progress)]


def add_root(mset, location: str) -> str:
    """Add a root to the mirror from the TUI's Add root…: a root of this
    mirror is opened; an empty folder (or one with no mirror) becomes a new,
    empty root of it; another mirror's root is refused. What happened."""
    from mmw.site import recents, ssh
    from mmw.site.home import could_become_root, init_root
    from mmw.site.mset import root_at
    remote = ssh.parse_location(location)[0] == "ssh"
    if not remote:
        location = str(Path(location).expanduser().resolve())   # remembered: the same from any folder
    found = root_at(location)
    if found is not None:
        if found.info.set_id != mset.set_id:
            raise ValueError(f"{location} holds the mirror of network {found.info.network!r} — pick another folder")
        recents.remember(location)
        # An add that failed part-way (an ssh root down mid-write) left some
        # roots not listing it: adding it again finishes the job.
        if any(found.info.name not in r.info.members for r in [*mset.roots, found]):
            _list_member(mset, found)
        return f"opened root {found.info.name} ({location})"
    taken = {r.info.name for r in mset.roots} | set(mset.missing)
    base = location.rstrip("/").rsplit("/", 1)[-1].split(":")[-1] or "root"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    if remote:
        from mmw.site.sshroot import empty_at, init_remote
        if not empty_at(location):
            raise ValueError(f"{location} isn't empty — pick an empty folder, or a new one")
        init_remote(location, name=name, set_id=mset.set_id, network=mset.network)
    else:
        if not could_become_root(Path(location)):
            raise ValueError(f"{location} isn't empty — pick an empty folder, or a new one")
        init_root(Path(location).expanduser(), name=name, set_id=mset.set_id, network=mset.network)
    recents.remember(location)       # first: a failure below is finished by adding it again
    _list_member(mset, root_at(location))
    return f"new root {name} at {location} — move repositories into it, or place new hostnames on it"


def _list_member(mset, new) -> None:
    """Every root of the mirror, and `new`, lists `new` as a member."""
    members = list(dict.fromkeys([*mset.names(), new.info.name]))
    for r in [*(r for r in mset.roots if r.info.name != new.info.name), new]:
        r.set_identity(members=members)


def root_details(home: Path) -> dict:
    """What a root says about itself for the TUI's detail: its HTTPS
    certificate (signed / none / expires) and its last apply."""
    from datetime import timezone
    out: dict = {"https": "no certificate yet", "last_apply": None, "last_apply_kind": None}
    roots = _roots(home)
    crt = Path(roots.primary.path) / ".mmw" / "tls" / "mirror.crt"
    if disk_io.ops.exists(crt):
        try:
            from cryptography import x509
            cert = x509.load_pem_x509_certificates(disk_io.ops.read_bytes(crt))[0]
            out["https"] = f"signed · expires {cert.not_valid_after_utc.astimezone(timezone.utc):%b %Y}"
        except (ValueError, OSError):
            out["https"] = "a certificate that can't be read — Certificates re-signs it"
    with SiteDB(home) as db:
        applied = db.applied()
    if applied:
        created, _prev, kind, _at = applied[0]
        out["last_apply"], out["last_apply_kind"] = created, kind
    # Every bundle applied here, newest first — what "As of a bundle" offers.
    out["applied"] = sorted({created for created, _p, kind, _a in applied if kind in ("full", "delta")},
                            reverse=True)
    return out


@dataclass(frozen=True)
class _Ident:
    ident: str


@dataclass(frozen=True)
class BundleStep:
    """One repository's part in serving the mirror as it was after a bundle:
    per suite, what it serves now and the point it had then."""
    root: object
    key: str
    label: str
    current: dict = field(default_factory=dict)     # suite → (date, ident) served now
    target: dict = field(default_factory=dict)      # suite → (date, ident) then; empty: left as it is
    note: str | None = None
    forward: bool = False                           # clear its pins and hold: each suite's newest

    @property
    def changes(self) -> bool:
        return self.forward or any(self.current.get(s) != t for s, t in self.target.items())

    def line(self) -> str:
        from mmw.site.versionref import day
        if self.note:
            return f"{self.label}: {self.note}"
        if not self.target:
            return f"{self.label}: not here yet then — left as it is"
        if not self.changes:
            return f"{self.label}: already as it was then"
        parts = [f"{s} {day(self.current[s][0]) if s in self.current else 'nothing'} → {day(t[0])}"
                 for s, t in sorted(self.target.items()) if self.current.get(s) != t]
        return f"{self.label}: {', '.join(parts)}"


def bundle_plan(mset, when: str) -> list[BundleStep]:
    """Every repository here, as it was once the bundle `when` names (its date,
    stamp or label) was applied: its newest version at or before that bundle."""
    from mmw.site.versionref import bundle_stamp, when_list
    repos = [(root, st, root.history(st.repo), root.deleted(st.repo))
             for root in mset.roots for st in root.status()]
    # Every point names the bundle it came in; a deleted one still does.
    moments = [VersionInfo(0, st.repo, 0, st.label, "", p.stamp, p.snap_label)
               for _r, st, hist, _d in repos for _t, rows in hist for p, *_ in rows]
    moments += [VersionInfo(0, st.repo, 0, st.label, "", d, None) for _r, st, _h, deleted in repos for d in deleted]
    stamp = bundle_stamp(moments, when)
    steps = []
    for root, st, hist, _deleted in repos:
        current, target, lost = {}, {}, []
        for t, rows in hist:
            suite = suite_of(t)
            for p, _total, _frees, mark in rows:
                if mark == "serving":
                    current[suite] = (p.date, p.ident)
            then = max(((p, mark) for p, *_x, mark in rows if p.stamp <= stamp),
                       key=lambda pm: (pm[0].date, pm[0].stamp, pm[0].ident), default=None)
            if then is None:
                continue
            if then[1] == "stub":
                # What it served then was deleted here; an older one would be a guess.
                lost.append((suite, then[0].stamp))
                continue
            target[suite] = (then[0].date, then[0].ident)
        note = None
        if lost:
            gone = ", ".join(f"{suite} ({when_list([stamp])[0]})" for suite, stamp in sorted(lost))
            note = (f"its point from then was deleted — left as it is: {gone}" if not target else
                    f"left as it is (its point from then was deleted): {gone}")
        steps.append(BundleStep(root, st.repo, st.label, current, target, note))
    steps.sort(key=lambda s: s.label.lower())
    for name in mset.missing:
        why = mset.missing_reason.get(name) or "not here"
        steps.append(BundleStep(None, "", f"root {name}", {}, {},
                                f"{why} — its repositories are left as they are"))
    return steps


def serve_bundle(steps: list[BundleStep], progress=None, stop_event=None) -> list[str]:
    """Serve each step that changes something; the labels served.
    `progress("repo", repo=)`, `("receive", bytes=1, name=)` per file placed
    (where a root can say), `("finished", repo=)`, then `("done")`. A stop is
    taken between repositories; it, or a failure part-way, says which were
    served and which weren't."""
    say = progress or (lambda *a, **k: None)
    todo = [s for s in steps if s.changes]
    done: list[str] = []
    held: list[str] = []
    for s in todo:
        if stop_event is not None and stop_event.is_set():
            rest = [t.label for t in todo if t.label not in done]
            raise ValueError(f"stopped — served: {', '.join(done) or 'none'}; not served: {', '.join(rest)}")
        say("repo", repo=s.key)
        try:
            from mmw.site.versionref import name_of
            r = None
            if s.forward:
                s.root.forward(s.key)
            for suite, (_date, ident) in ([] if s.forward else sorted(s.target.items())):
                if s.current.get(suite) == (_date, ident):
                    continue
                r = s.root.serve(s.key, name_of(_Ident(ident)), suite,
                                 progress=lambda rel: say("receive", bytes=1, name=rel))
                if getattr(r, "held", None):          # its site layer could not be built
                    break
            if getattr(r, "held", None):
                held.append(f"{s.label} ({r.held})")
                continue
        except Exception as e:
            if not done:
                raise
            rest = [t.label for t in todo if t.label not in done]
            raise ValueError(f"{s.label}: {e} — served: {', '.join(done)}; not served: {', '.join(rest)}") from e
        say("finished", repo=s.key)
        done.append(s.label)
    say("done")
    if held:
        raise ValueError(f"served: {', '.join(done) or 'none'}; held by local packages: {'; '.join(held)}")
    return done
