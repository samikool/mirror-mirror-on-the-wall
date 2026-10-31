"""Switch a repository's served tree from one version to another.

Only the paths that differ are touched: new and changed files are placed —
packages, then indexes, then release files — and files the target lacks are
removed last, so an apt client mid-update never sees an index naming a file
that is not there, nor a file vanish an index still lists. Every placement is
staged and renamed in. A switch is recorded before it starts and cleared when
it ends; one cut short is finished by `resume_switches`.
"""
from dataclasses import dataclass
from pathlib import Path

from mmw.core import disk_io
from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE
from mmw.core.publisher import replay_kind
from mmw.resources.apply import _prune_empty_parents

PHASES = (KIND_PKG, KIND_META, KIND_RELEASE)


@dataclass(frozen=True)
class SwitchResult:
    placed: int
    removed: int
    held: str | None = None              # not switched: why (a site layer that cannot be built)
    warnings: tuple[str, ...] = ()


def _base(rel: str, bases: list[str]) -> str | None:
    return max((b for b in bases if rel.startswith(b + "/")), key=len, default=None)


def _run(db, store, root: Path, repo: str, from_id: int | None, to_id: int, progress,
         overlay: str | None = None) -> SwitchResult:
    if db.version_by_id(to_id) is None:
        # Never diff against nothing: that would remove the repository's
        # whole served tree.
        db.end_switch(repo)
        raise ValueError(f"version {to_id} of {repo} is no longer here — not switching to it")
    state = db.repo_state(repo)
    # What is served is a version plus its site layer's overlay, both sides.
    before = db.served_files(from_id, db.repo_overlay(repo)) if from_id is not None else {}
    after = db.served_files(to_id, overlay)
    changed = {p: c for p, c in after.items() if before.get(p) != c}
    by_kind: dict[str, list[str]] = {k: [] for k in PHASES}
    for rel in sorted(changed):
        by_kind[replay_kind(rel, _base(rel, state.bases))].append(rel)
    placed = 0
    gone = set(before) - set(after)
    # A release file that goes (an unsigned layer drops InRelease) goes with
    # the release phase, not after: never upstream's signature beside new indexes.
    release_gone = sorted(r for r in gone if replay_kind(r, _base(r, state.bases)) == KIND_RELEASE)
    removed = 0
    for kind in PHASES:
        db.switch_phase(repo, kind)
        for rel in by_kind[kind]:
            store.place(changed[rel], root / rel)
            placed += 1
            if progress:
                progress(rel, kind)
        if kind == KIND_RELEASE:
            for rel in release_gone:
                if disk_io.ops.remove(root / rel) is not None:
                    removed += 1
    db.switch_phase(repo, "delete")
    for rel in sorted(gone - set(release_gone)):
        if disk_io.ops.remove(root / rel) is not None:
            removed += 1
            _prune_empty_parents(str(root), str((root / rel).parent))
    newest = db.versions(repo)[0].id
    db.set_overlay(repo, overlay, commit=False)
    db.set_serving(repo, to_id, held=to_id != newest)
    db.set_meta(f"hold:{repo}", "")
    db.retire_overlays(repo, to_id, overlay)
    db.end_switch(repo)
    return SwitchResult(placed, removed)


def switch_in(db, roots, repo: str, to_id: int, *, progress=None) -> SwitchResult:
    """`switch_repo` in the repository's own root."""
    root = roots.of(db.repo_state(repo))
    return switch_repo(db, roots.store(root), Path(root.path), repo, to_id, progress=progress)


def switch_repo(db, store, root: Path, repo: str, to_id: int, *, progress=None) -> SwitchResult:
    """Serve version `to_id` of `repo` at `root`. Held afterwards when it is
    not the newest version here."""
    if db.version_by_id(to_id) is None:
        raise ValueError(f"version {to_id} of {repo} is not here")
    state = db.repo_state(repo)
    from_id = state.serving.id if state and state.serving else None
    # A site layer goes on top of the version first; one that cannot be
    # built (no key, upstream signing with debs) keeps what is served, held.
    from mmw.site.layers import build_overlay
    layered = build_overlay(db.home, db, store, repo, to_id)
    if layered.hold_reason:
        db.set_held(repo, True)
        db.set_meta(f"hold:{repo}", layered.hold_reason)
        return SwitchResult(0, 0, held=layered.hold_reason, warnings=tuple(layered.warnings))
    db.begin_switch(repo, from_id, to_id, layered.digest)
    result = _run(db, store, Path(root), repo, from_id, to_id, progress, layered.digest)
    return SwitchResult(result.placed, result.removed, warnings=tuple(layered.warnings))


def resume_switches(db, roots) -> list[str]:
    """Finish every switch a killed run left halfway, each in its
    repository's root (`roots`: a `mmw.site.roots.Roots`)."""
    done = []
    for repo, from_id, to_id, _phase in db.pending_switches():
        root = roots.of(db.repo_state(repo))
        try:
            roots.check_mounted(root)
        except FileNotFoundError:
            continue                    # finished once that root is back
        try:
            _run(db, roots.store(root), Path(root.path), repo, from_id, to_id, None,
                 db.switch_overlay(repo))
        except ValueError:
            continue                    # its target is gone; what it served stays
        done.append(repo)
    return done
