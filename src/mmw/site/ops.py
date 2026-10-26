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
            from mmw.site.space import real_free
            usage = disk_io.ops.disk_usage(r.path)
            rep.roots[r.name] = (usage.used, real_free(r.path))      # on WSL, the Windows drive's too
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
        return {"label": state.label, "bases": state.bases, "held": state.held,
                "serving": state.serving.number if state.serving else None, "versions": versions,
                "hosts": sorted(hosts),
                "sharing": [s.label for s in db.repo_states() if s.repo != key and hosts & _hosts_of(s.bases)]}


def move_in(home: Path, key: str, info: dict) -> bool:
    """Record a moved repository's versions here (blobs already in the
    store), place its served version; whether nginx changed."""
    from mmw.core.publisher import replay_kind
    from mmw.site.apply import place_served_file, write_root_nginx
    from mmw.site.switch import PHASES, _base
    from mmw.resources.apply import is_safe_path
    for v in info["versions"]:                       # another root's word: checked, never trusted
        for rel, c, _size in v["files"]:
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
        return changed


def move_out_finish(home: Path, key: str, to: str, info: dict) -> bool:
    """The repository lives in `to` now: forget it here, keeping its served
    files until `tidy`; whether nginx changed."""
    import json
    from mmw.site.apply import write_root_nginx
    served = next((v for v in info["versions"] if v["number"] == info["serving"]), None)
    with _session(home) as (db, roots):
        db.set_meta(f"moved:{key}", json.dumps({"to": to, "label": info["label"],
                                                "files": sorted(rel for rel, _c, _s in served["files"])
                                                if served else []}))
        db.drop_repo(key)
        return bool(roots.multi and db.get_meta("settings") and write_root_nginx(db, roots))


def set_moving_in(home: Path, key: str, src: str) -> None:
    with SiteLock(home), SiteDB(home) as db:
        db.set_meta(f"moving_in:{key}", src)


def tidy_here(home: Path) -> list[str]:
    """Clear moved repositories' old served files here; the labels tidied."""
    import json
    from mmw.resources.apply import _prune_empty_parents
    done = []
    with _session(home) as (db, roots):
        path = Path(roots.primary.path)
        here = {p for s in db.repo_states() if s.serving is not None for p in db.version_files(s.serving.id)}
        for k, raw in db.metas("moved:").items():
            rec = json.loads(raw or "{}")
            if not rec:
                continue
            for rel in rec.get("files", []):
                if rel not in here and disk_io.ops.remove(path / rel) is not None:
                    _prune_empty_parents(str(path), str((path / rel).parent))
            db.set_meta(k, "{}")
            done.append(rec.get("label", k.split(":", 1)[1]))
        roots.store(roots.primary).gc(roots.referenced(db, roots.primary))
    return done


def move_size(mset, ref: str, to_root: str) -> int:
    """Bytes moving `ref` to `to_root` copies (what that root lacks)."""
    src, key = mset.root_for_repo(ref)
    info = src.move_out_info(key)
    sizes = {c: size for v in info["versions"] for _rel, c, size in v["files"]}
    return sum(sizes[c] for c in set(sizes) - mset.root_named(to_root).has(set(sizes)))


def move_repo(mset, ref: str, to_root: str) -> MoveResult:
    """Move a repository, every kept version, to another root of the set.
    Its files are copied into the target's store and its served version
    placed there before the old root lets go — a move cut short just runs
    again. The old root keeps serving its copy (the nginx running there
    still routes the hostname to it) until `tidy_set`, once nginx has been
    recreated in both roots."""
    from mmw.core.format import fmt_size
    src, key = mset.root_for_repo(ref)
    dst = mset.root_named(to_root)
    if src.info.name == dst.info.name:
        raise ValueError(f"{ref} is already in {dst.info.name}")
    info = src.move_out_info(key)
    if info["sharing"]:
        raise ValueError(f"{info['label']} shares {', '.join(info['hosts'])} with {', '.join(info['sharing'])} — "
                         "a hostname lives in one root; move them together")
    sizes = {c: size for v in info["versions"] for _rel, c, size in v["files"]}
    lacking = set(sizes) - dst.has(set(sizes))
    need, free = sum(sizes[c] for c in lacking), dst.free_bytes()
    if need > free:
        raise ValueError(f"moving {info['label']} needs {fmt_size(need)} in {dst.info.name}, which has "
                         f"{fmt_size(free)} free — nothing was copied")
    # Until the old root lets go, the copy is not the repository's home: a
    # move cut short runs again from the old root.
    dst.set_moving_in(key, src.info.name)
    for c in sorted(lacking):
        with src.export_blob(c) as fh:
            dst.import_blob(fh, c)
    changed = [dst.info.name] if dst.move_in(key, info) else []
    if src.move_out_finish(key, dst.info.name, info):
        changed.insert(0, src.info.name)
    dst.set_moving_in(key, "")
    for root in mset.roots:
        root.set_identity(hosts={**root.hosts_map(), **{h: dst.info.name for h in info["hosts"]}})
    return MoveResult(need, len(lacking), changed, dst.tls_notes() + src.tls_notes())


def tidy_set(mset) -> list[str]:
    """Clear moved repositories' old copies (once nginx has been recreated);
    the labels tidied."""
    return [label for root in mset.roots for label in root.tidy()]


def add_root(mset, location: str) -> str:
    """Add a root to the mirror from the TUI's Add root…: a root of this
    mirror is opened; an empty folder (or one with no mirror) becomes a new,
    empty root of it; another mirror's root is refused. What happened."""
    from mmw.site import recents, ssh
    from mmw.site.home import init_root
    from mmw.site.mset import root_at
    found = root_at(location)
    if found is not None:
        if found.info.set_id != mset.set_id:
            raise ValueError(f"{location} holds the mirror of network {found.info.network!r} — pick another folder")
        recents.remember(location)
        return f"opened root {found.info.name} ({location})"
    taken = {r.info.name for r in mset.roots} | set(mset.missing)
    base = location.rstrip("/").rsplit("/", 1)[-1].split(":")[-1] or "root"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    if ssh.parse_location(location)[0] == "ssh":
        from mmw.site.sshroot import init_remote
        init_remote(location, name=name, set_id=mset.set_id, network=mset.network)
    else:
        init_root(Path(location).expanduser(), name=name, set_id=mset.set_id, network=mset.network)
    new = root_at(location)
    members = list(dict.fromkeys([*mset.roots[0].info.members, *mset.missing, name]))
    for r in [*mset.roots, new]:
        r.set_identity(members=members)
    recents.remember(location)
    return f"new root {name} at {location} — move repositories into it, or place new hostnames on it"


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
