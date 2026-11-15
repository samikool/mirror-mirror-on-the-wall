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


def _reserve(db: SiteDB, roots: Roots, repo: str, progress=None, *, again: bool = False) -> SwitchResult:
    """Serve what the timeline chooses now (pins, else newest complete).
    `again`: switch even to what is served — its site layer is rebuilt."""
    from mmw.site.timeline import choose, ensure_state
    state = db.repo_state(repo)
    store = roots.store(roots.of(state))
    chosen, warnings = choose(db, store, repo)
    if not chosen:
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


def _render(roots: Roots) -> None:
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
        _render(roots)
        return result


def _pinned(db: SiteDB, repo: str) -> bool:
    return any(t.pinned is not None for t in db.tracks(repo))


def forward(home: Path, ref: str | None) -> list[str]:
    """Clear a repository's hold and pins — one, or (None) every held or
    pinned one — and serve each suite's newest. The repositories moved."""
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
            if _reserve(db, roots, repo).held:
                continue                        # still held: its layer cannot be built yet
            moved.append(repo)
        if moved:
            _render(roots)
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
        result = switch_in(db, roots, repo, target,
                           progress=(lambda rel, _kind: progress(rel)) if progress else None)
        if not result.held and not layer_held:
            db.set_held(repo, held)              # a re-layer is not a fast-forward
        _render(roots)
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


def _gc(db: SiteDB, roots: Roots) -> int:
    db.set_complete(None, None)              # the store changes: completeness is checked afresh
    freed = 0
    for r in roots.all:
        if disk_io.ops.is_dir(Path(r.path) / ".mmw" / "store"):
            freed += roots.store(r).gc(roots.referenced(db, r))[1]
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
    return None


def delete_point(home: Path, ref: str, when: str, track: str | None = None, force: bool = False) -> int:
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
        if why and not (why[0] == "served" and force):
            raise ValueError(why[1])
        db.stub_point(p.id)
        if why and why[0] == "served":
            _reserve(db, roots, repo)
            _render(roots)
        db.drop_unused_states(repo)
        return _gc(db, roots)


def apply_retention(db: SiteDB, roots: Roots, keep: int) -> list[tuple[str, str, str]]:
    """Per suite, delete the points past the newest `keep` — never a
    protected one (served, served before, pinned, mid-switch). The
    (repository, suite, date) deleted."""
    from mmw.site.versionref import day
    gone = []
    for st in db.repo_states():
        for t in db.tracks(st.repo):
            for pt in db.points(t.id)[keep:]:
                if _protected(db, st.repo, t, pt) is None:
                    db.stub_point(pt.id)
                    gone.append((st.repo, suite_of(t), day(pt.date)))
    if gone:
        _gc(db, roots)
    return gone


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
                mark = ("serving" if pt.id in served else "pinned" if t.pinned == pt.id
                        else "previous" if t.previous == pt.id else "stub" if not pt.held
                        else "incomplete" if is_complete(db, store, pt.id) else "")
                rows.append((pt, total, 0 if not pt.held else frees, mark))
            if rows:
                out.append((t, rows))
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


def set_keep(mset, keep: int | None) -> None:
    """Keep the newest `keep` versions of each repository (None: all), in every root here."""
    for root in mset.roots:
        root.keep(keep)


def keep_here(home: Path, keep: int | None) -> None:
    from mmw.site.home import save_settings
    with _session(home) as (db, roots):
        save_settings(home, load_site(home).model_copy(update={"keep_versions": keep}))
        if keep is not None:
            apply_retention(db, roots, keep)


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
                           "pinned": byid.get(t.pinned), "previous": byid.get(t.previous)})
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
        _render(roots)
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
                lost.append(then[0].stamp)
                continue
            target[suite] = (then[0].date, then[0].ident)
        note = None
        if lost and not target:
            note = f"its point from then ({when_list([max(lost)])[0]}) was deleted — left as it is"
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
