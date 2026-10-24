"""Per-repository deltas, for bundles to a far side running mmw.

Each repository travels from the version the far side was last sent (or
an explicit starting snapshot's) to its version in the destination
snapshot. A repository already at its destination is left out; one whose
starting version is no longer kept here is sent in full, with a warning.
"""
from dataclasses import dataclass

from mmw.core.config import Config, NetworkConfig
from mmw.core.pool import Pool, RepoVersionInfo, SnapshotInfo

_LAST = object()        # from_ref default: where the last bundle of each repository went


@dataclass(frozen=True)
class RepoDelta:
    repo: str
    label: str
    start: RepoVersionInfo | None       # None: sent in full
    dest: RepoVersionInfo
    bases: list[str]


def plan_repo_deltas(pool: Pool, network: NetworkConfig, cfg: Config, dest: SnapshotInfo, *,
                     repos: list[str] | None, from_ref) -> tuple[list[RepoDelta], list[str]]:
    """The repositories to send from `dest`, each with where it starts, and
    the warnings to report. `from_ref`: `_LAST`, None (all in full), or a
    snapshot. Raises LookupError for an unknown repository and ValueError
    when there is nothing to send."""
    from mmw.core.repos import repository_label
    from mmw.core.snapshots import recorded_repos, repo_version_files
    versions = pool.snapshot_repo_versions(dest.id)
    repo_bases = recorded_repos(dest.settings)
    if repos is None:
        chosen = sorted(versions)
    else:
        unknown = [r for r in repos if r not in versions]
        if unknown:
            have = ", ".join(sorted(versions))
            raise LookupError(f"unknown repository {unknown[0]!r} — this network has: {have}")
        chosen = list(dict.fromkeys(repos))

    warnings: list[str] = []
    if from_ref is _LAST:
        sent = pool.last_sent_versions(network.name)
        starts = {r: pool.repo_version(sent[r]) for r in chosen if r in sent}
    elif from_ref is None:
        starts = {}
    else:
        if not from_ref.complete:
            warnings.extend(f"{repository_label(r, cfg)}: {from_ref.stamp} is history only — "
                            "sending it in full" for r in chosen)
            starts = {}
        else:
            starts = pool.snapshot_repo_versions(from_ref.id)

    deltas = []
    for repo in chosen:
        label = repository_label(repo, cfg)
        start, end = starts.get(repo), versions[repo]
        if start is not None and start.id == end.id:
            continue                                # already there
        if start is not None and repo_version_files(pool, start) is None:
            warnings.append(f"{label}: v{start.number} is no longer kept here — sending it in full")
            start = None
        deltas.append(RepoDelta(repo, label, start, end, repo_bases.get(repo, [])))
    if not deltas:
        raise ValueError("nothing to send — every chosen repository is already at its destination version")
    return deltas, warnings
