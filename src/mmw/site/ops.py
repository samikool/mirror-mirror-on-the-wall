"""What an operator does to a site between bundles: serve any kept version
of a repository, hold it where it is, move it forward."""
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from mmw.core import disk_io
from mmw.core.repos import resolve_repo_refs
from mmw.resources.apply import render_derived
from mmw.site.db import SiteDB, VersionInfo
from mmw.site.home import load_site
from mmw.site.lock import SiteLock
from mmw.site.store import Store
from mmw.site.roots import Roots
from mmw.site.switch import SwitchResult, resume_switches, switch_in


def resolve_repo(db: SiteDB, ref: str) -> str:
    """A repository of this site by key, or by label in any case."""
    return resolve_repo_refs([ref], {s.repo: s.label for s in db.repo_states()})[0]


def _version(db: SiteDB, repo: str, number: int) -> VersionInfo:
    found = db.version(repo, number)
    if found is None:
        have = ", ".join(f"v{v.number}" for v in db.versions(repo))
        raise LookupError(f"{db.repo_state(repo).label} has no v{number} here — it has {have}")
    return found


@dataclass(frozen=True)
class ServePreview:
    label: str
    current: VersionInfo | None
    target: VersionInfo
    relinks: int


def serve_preview(home: Path, ref: str, number: int) -> ServePreview:
    """What `serve` would do, for its confirmation."""
    with SiteDB(home) as db:
        repo = resolve_repo(db, ref)
        state, target = db.repo_state(repo), _version(db, repo, number)
        before = db.version_files(state.serving.id) if state.serving else {}
        after = db.version_files(target.id)
        changed = sum(1 for p, c in after.items() if before.get(p) != c) + len(set(before) - set(after))
        return ServePreview(state.label, state.serving, target, changed)


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
        resume_switches(db, roots)
        yield db, roots


def serve(home: Path, ref: str, number: int) -> SwitchResult:
    """Serve version `number` of a repository; held unless it is the newest."""
    with _session(home) as (db, roots):
        repo = resolve_repo(db, ref)
        roots.check_mounted(roots.of(db.repo_state(repo)))
        result = switch_in(db, roots, repo, _version(db, repo, number).id)
        _render(roots)
        return result


def forward(home: Path, ref: str | None) -> list[str]:
    """Serve the newest version of one repository, or (None) of every held
    one, and clear the hold. The repositories moved."""
    moved = []
    with _session(home) as (db, roots):
        repos = [resolve_repo(db, ref)] if ref is not None else [s.repo for s in db.repo_states() if s.held]
        for repo in repos:
            newest = db.versions(repo)[0]
            state = db.repo_state(repo)
            roots.check_mounted(roots.of(state))
            if state.serving is None or state.serving.id != newest.id:
                switch_in(db, roots, repo, newest.id)
            else:
                db.set_held(repo, False)
            moved.append(repo)
        if moved:
            _render(roots)
    return moved


def hold(home: Path, ref: str) -> str:
    """Keep a repository where it is: the next bundle stores its new version
    without serving it."""
    with _session(home) as (db, _roots_):
        repo = resolve_repo(db, ref)
        db.set_held(repo, True)
        return db.repo_state(repo).label


# ── space ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VersionSpace:
    number: int
    stamp: str
    snap_label: str | None
    total: int              # bytes its files take
    frees: int              # bytes deleting it would free (used by no other version)
    serving: bool


@dataclass
class SpaceReport:
    repos: dict[str, list[VersionSpace]] = field(default_factory=dict)   # newest first
    labels: dict[str, str] = field(default_factory=dict)
    used: int = 0               # the primary root's
    free: int = 0
    history_bytes: int = 0      # what deleting every unserved version would free
    roots: dict[str, tuple[int, int]] = field(default_factory=dict)   # name → (used, free)
    placed: dict[str, str] = field(default_factory=dict)              # repository → root name


def space(home: Path) -> SpaceReport:
    roots = _roots(home)
    rep = SpaceReport()
    with SiteDB(home) as db:
        for st in db.repo_states():
            rep.placed[st.repo] = roots.of(st).name
            rows = []
            for v in db.versions(st.repo):
                total, frees = db.version_sizes(v.id)
                serving = st.serving is not None and st.serving.id == v.id
                rows.append(VersionSpace(v.number, v.stamp, v.snap_label, total, frees, serving))
                if not serving:
                    rep.history_bytes += frees
            rep.repos[st.repo], rep.labels[st.repo] = rows, st.label
    for r in roots.all:
        if disk_io.ops.is_dir(r.path):
            usage = disk_io.ops.disk_usage(r.path)
            rep.roots[r.name] = (usage.used, usage.free)
    rep.used, rep.free = rep.roots.get(roots.primary.name, (0, 0))
    return rep


def _gc(db: SiteDB, roots: Roots) -> int:
    freed = 0
    for r in roots.all:
        if disk_io.ops.is_dir(Path(r.path) / ".mmw" / "store"):
            freed += roots.store(r).gc(roots.referenced(db, r))[1]
    return freed


def _protected(db: SiteDB, repo: str, version: VersionInfo) -> str | None:
    """Why a version cannot be deleted, or None."""
    state = db.repo_state(repo)
    if state.serving is not None and state.serving.id == version.id:
        return f"v{version.number} is being served — serve another version first"
    if state.held and db.versions(repo)[0].id == version.id:
        return f"v{version.number} is the newest of a held repository — forward it, or delete it after"
    if any(to_id == version.id for _r, _f, to_id, _p in db.pending_switches()):
        return f"v{version.number} is what an unfinished switch is going to"
    if len(db.versions(repo)) == 1:
        return f"v{version.number} is the only version of {state.label} here"
    return None


def delete_version(home: Path, ref: str, number: int) -> int:
    """Drop a kept version and the files nothing else uses; bytes freed."""
    with _session(home) as (db, roots):
        repo = resolve_repo(db, ref)
        version = _version(db, repo, number)
        why = _protected(db, repo, version)
        if why:
            raise ValueError(why)
        db.delete_version(version.id)
        return _gc(db, roots)


def apply_retention(db: SiteDB, roots: Roots, keep: int) -> list[tuple[str, int]]:
    """Per repository, delete the versions past the newest `keep` — never a
    protected one. The (repository, number) pairs deleted."""
    gone = []
    for st in db.repo_states():
        for v in db.versions(st.repo)[keep:]:
            if _protected(db, st.repo, v) is None:
                db.delete_version(v.id)
                gone.append((st.repo, v.number))
    if gone:
        _gc(db, roots)
    return gone


# ── status, history, verify ──────────────────────────────────────────────────

def status(home: Path):
    """Every repository's state, by label."""
    with SiteDB(home) as db:
        return db.repo_states()


def history(home: Path, ref: str) -> list[tuple[VersionInfo, int, int, bool]]:
    """A repository's kept versions, newest first: (version, total bytes,
    bytes deleting it frees, serving)."""
    with SiteDB(home) as db:
        repo = resolve_repo(db, ref)
        state = db.repo_state(repo)
        out = []
        for v in db.versions(repo):
            total, frees = db.version_sizes(v.id)
            out.append((v, total, frees, state.serving is not None and state.serving.id == v.id))
        return out


def _expected(db: SiteDB, roots: Roots) -> dict[str, dict[str, str]]:
    """Root name → {path: checksum} it should serve."""
    out: dict[str, dict[str, str]] = {r.name: {**roots.network_files(db, r), **roots.generated(db, r)}
                                      for r in roots.all}
    for st in db.repo_states():
        if st.serving is not None:
            out[roots.of(st).name].update(db.version_files(st.serving.id))
    return out


def _wrong(root: Path, store: Store, expected: dict[str, str]):
    """(rel, checksum, what is wrong) for every served file not as it should be."""
    import hashlib
    from mmw.resources.apply import ROOT_PLACEHOLDER, _nginx_root_config
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
                from mmw.site.apply import _ensure
                _ensure(roots, store, checksum)
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


def move_size(home: Path, ref: str, root_name: str) -> int:
    """Bytes `move` would copy into the target root's store."""
    roots = _roots(home)
    with SiteDB(home) as db:
        repo = resolve_repo(db, ref)
        dst = roots.store(roots.named(root_name))
        need = {c for v in db.versions(repo) for c in db.version_files(v.id).values() if not dst.has(c)}
        src = roots.store(roots.of(db.repo_state(repo)))
        return sum(disk_io.ops.stat(src.path(c)).st_size for c in need if src.has(c))


def move(home: Path, ref: str, root_name: str) -> MoveResult:
    """Move a repository to another root. Every kept version's files are
    copied into its store and the served ones placed there before anything
    is recorded — so a move cut short just runs again. The old root keeps
    its copy, still served by the nginx running there, until `tidy` (or the
    next apply), once nginx has been recreated."""
    import json
    from mmw.site.apply import _ensure, check_shared_hosts, place_served_file, write_root_nginx
    with _session(home) as (db, roots):
        repo = resolve_repo(db, ref)
        state = db.repo_state(repo)
        src, dst = roots.of(state), roots.named(root_name)
        if src.name == dst.name:
            raise ValueError(f"{state.label} is already in {dst.name}")
        roots.check_mounted(src)
        roots.check_mounted(dst)
        states = db.repo_states()
        check_shared_hosts({s.repo: (dst if s.repo == repo else roots.of(s)) for s in states},
                           {s.repo: s.label for s in states}, {s.repo: s.bases for s in states})
        dst_store, dst_path = roots.store(dst), Path(dst.path)
        copied = files = 0
        for v in db.versions(repo):
            for checksum in db.version_files(v.id).values():
                if not dst_store.has(checksum):
                    _ensure(roots, dst_store, checksum)
                    copied += disk_io.ops.stat(dst_store.path(checksum)).st_size
                    files += 1
        if state.serving is not None:                 # placed, in package → index → release order
            from mmw.core.publisher import replay_kind
            from mmw.site.switch import PHASES, _base
            served = db.version_files(state.serving.id)
            for kind in PHASES:
                for rel in sorted(served):
                    if replay_kind(rel, _base(rel, state.bases)) == kind:
                        dst_store.place(served[rel], dst_path / rel)
        for rel, checksum in roots.network_files(db, dst).items():   # keys, the nginx image
            if not disk_io.ops.exists(dst_path / rel):
                _ensure(roots, dst_store, checksum)
                place_served_file(dst_store, dst_path, rel, checksum, network=True)
        # Recorded in one go: the new root, and what the old one still holds.
        db.set_meta(f"move:{repo}", json.dumps({"from": src.name,
                                               "version": state.serving.id if state.serving else None}))
        db.set_root(repo, dst.name)
        changed = []
        if roots.multi and db.get_meta("settings") and write_root_nginx(db, roots):
            changed = [src.name, dst.name]
        _render(roots)
        return MoveResult(copied, files, changed)


def tidy(home: Path) -> list[str]:
    """Clear the old roots of moved repositories (do it once nginx has been
    recreated); the labels of the repositories tidied."""
    with _session(home) as (db, roots):
        return _tidy(db, roots)


def _tidy(db: SiteDB, roots: Roots) -> list[str]:
    import json
    from mmw.resources.apply import _prune_empty_parents
    done = []
    for st in db.repo_states():
        rec = json.loads(db.get_meta(f"move:{st.repo}") or "{}")
        if not rec:
            continue
        old = roots.named(rec["from"])
        root = Path(old.path)
        if not disk_io.ops.is_dir(root / ".mmw" / "store"):
            continue                                  # not mounted: tidied when it is
        here = {p for s in db.repo_states() if s.serving is not None and roots.of(s).name == old.name
                for p in db.version_files(s.serving.id)}
        if rec.get("version") is not None:
            for rel in db.version_files(rec["version"]):
                if rel not in here and disk_io.ops.remove(root / rel) is not None:
                    _prune_empty_parents(str(root), str((root / rel).parent))
        roots.store(old).gc(roots.referenced(db, old))
        db.set_meta(f"move:{st.repo}", "{}")
        done.append(st.label)
    return done
