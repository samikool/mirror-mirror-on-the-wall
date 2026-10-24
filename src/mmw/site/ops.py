"""What an operator does to a site between bundles: serve any kept version
of a repository, hold it where it is, move it forward."""
from dataclasses import dataclass
from pathlib import Path

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


def serve(home: Path, ref: str, number: int) -> SwitchResult:
    """Serve version `number` of a repository; held unless it is the newest."""
    root, store = _open(home)
    with SiteLock(home), SiteDB(home) as db:
        resume_switches(db, store, root)
        repo = resolve_repo(db, ref)
        result = switch_repo(db, store, root, repo, _version(db, repo, number).id)
        render_derived(str(root))
        return result


def forward(home: Path, ref: str | None) -> list[str]:
    """Serve the newest version of one repository, or (None) of every held
    one, and clear the hold. The repositories moved."""
    root, store = _open(home)
    moved = []
    with SiteLock(home), SiteDB(home) as db:
        resume_switches(db, store, root)
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
    with SiteLock(home), SiteDB(home) as db:
        repo = resolve_repo(db, ref)
        db.set_held(repo, True)
        return db.repo_state(repo).label
