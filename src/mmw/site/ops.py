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
from mmw.site.switch import SwitchResult, resume_switches, switch_repo


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


def _open(home: Path):
    settings = load_site(home)
    root = Path(settings.root)
    return root, Store(root)


@contextmanager
def _session(home: Path):
    """Lock the site, open its database, and finish any switch a killed run
    left halfway — before anything else looks at what is served."""
    root, store = _open(home)
    with SiteLock(home), SiteDB(home) as db:
        resume_switches(db, store, root)
        yield db, store, root


def serve(home: Path, ref: str, number: int) -> SwitchResult:
    """Serve version `number` of a repository; held unless it is the newest."""
    with _session(home) as (db, store, root):
        repo = resolve_repo(db, ref)
        result = switch_repo(db, store, root, repo, _version(db, repo, number).id)
        render_derived(str(root))
        return result


def forward(home: Path, ref: str | None) -> list[str]:
    """Serve the newest version of one repository, or (None) of every held
    one, and clear the hold. The repositories moved."""
    moved = []
    with _session(home) as (db, store, root):
        repos = [resolve_repo(db, ref)] if ref is not None else [s.repo for s in db.repo_states() if s.held]
        for repo in repos:
            newest = db.versions(repo)[0]
            state = db.repo_state(repo)
            if state.serving is None or state.serving.id != newest.id:
                switch_repo(db, store, root, repo, newest.id)
            else:
                db.set_held(repo, False)
            moved.append(repo)
        if moved:
            render_derived(str(root))
    return moved


def hold(home: Path, ref: str) -> str:
    """Keep a repository where it is: the next bundle stores its new version
    without serving it."""
    with _session(home) as (db, _store, _root):
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
    used: int = 0
    free: int = 0
    history_bytes: int = 0      # what deleting every unserved version would free


def space(home: Path) -> SpaceReport:
    root, _store = _open(home)
    rep = SpaceReport()
    with SiteDB(home) as db:
        for st in db.repo_states():
            rows = []
            for v in db.versions(st.repo):
                total, frees = db.version_sizes(v.id)
                serving = st.serving is not None and st.serving.id == v.id
                rows.append(VersionSpace(v.number, v.stamp, v.snap_label, total, frees, serving))
                if not serving:
                    rep.history_bytes += frees
            rep.repos[st.repo], rep.labels[st.repo] = rows, st.label
    usage = disk_io.ops.disk_usage(root)
    rep.used, rep.free = usage.used, usage.free
    return rep


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
    with _session(home) as (db, store, _root):
        repo = resolve_repo(db, ref)
        version = _version(db, repo, number)
        why = _protected(db, repo, version)
        if why:
            raise ValueError(why)
        db.delete_version(version.id)
        return store.gc(db.referenced_checksums())[1]


def apply_retention(db: SiteDB, store: Store, keep: int) -> list[tuple[str, int]]:
    """Per repository, delete the versions past the newest `keep` — never a
    protected one. The (repository, number) pairs deleted."""
    gone = []
    for st in db.repo_states():
        for v in db.versions(st.repo)[keep:]:
            if _protected(db, st.repo, v) is None:
                db.delete_version(v.id)
                gone.append((st.repo, v.number))
    if gone:
        store.gc(db.referenced_checksums())
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


def _expected(db: SiteDB) -> dict[str, str]:
    expected: dict[str, str] = dict(db.network_files())
    for st in db.repo_states():
        if st.serving is not None:
            expected.update(db.version_files(st.serving.id))
    return expected


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
    root, store = _open(home)
    with SiteDB(home) as db:
        expected = _expected(db)
    return [f"{rel}: {why}" for rel, _cs, why in _wrong(root, store, expected)]


def repair(home: Path) -> int:
    """Put every wrong or missing served file back from the store; how many."""
    from mmw.site.apply import place_served_file
    fixed = 0
    with _session(home) as (db, store, root):
        network = set(db.network_files())
        for rel, checksum, _why in list(_wrong(root, store, _expected(db))):
            if disk_io.ops.exists(root / rel):
                disk_io.ops.remove(root / rel)        # never write through a damaged link
            place_served_file(store, root, rel, checksum, network=rel in network)
            fixed += 1
    return fixed
