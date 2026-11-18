"""`mmw sync --as-of DATE --repo NAME` / `--from-url URL`: a Debian or Ubuntu
repository's past state, from its snapshot archive, recorded as points.

`look` is what both the CLI and the TUI's Add a past point page show before
anything is fetched; `run` is the CLI's headless path. The fetch and the
recording are `core.past_points`, shared with the TUI's sync page.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from mmw.commands.common import eprint, interruptible, load_config_or_none
from mmw.core import past_points as pp
from mmw.core.config import MirrorConfig


@dataclass
class Target:
    """One repository's past to record: where from, and what it covers."""
    archive: pp.Archive
    name: str
    stamp: str
    repo: str
    label: str
    suites: list[str]

    @property
    def base(self) -> str:
        return self.archive.base(self.name, self.stamp)

    def describe(self) -> str:
        from mmw.site.versionref import day
        when = f"{day(self.stamp)} {self.stamp[9:11]}:{self.stamp[11:13]}:{self.stamp[13:15]} UTC"
        return (f"{self.label} ({', '.join(self.suites)}) from {self.archive.host}"
                f"{self.archive.prefix}/{self.name} as of {when}")


def _fetch(url: str) -> bytes | None:
    return pp.fetch_release(url)


def apt_repos(cfg, mirror_dir) -> dict[str, list[MirrorConfig]]:
    """Every configured APT repository → all its configured and pooled units."""
    from mmw.core.repos import repository_key
    from mmw.core.resolve import available_mirrors
    out: dict[str, dict[str, MirrorConfig]] = {}
    for m in [*cfg.mirrors, *available_mirrors(cfg, mirror_dir)]:
        if m.provider == "apt":
            out.setdefault(repository_key(m), {})[m.name] = m
    return {k: sorted(v.values(), key=lambda u: u.name) for k, v in sorted(out.items())}


def resolve_repo(cfg, repos: dict[str, list[MirrorConfig]], ref: str) -> str:
    """The key of the apt repository `ref` names (its key or its name)."""
    from mmw.core.repos import repository_label, resolve_repo_refs
    names = {k: repository_label(k, cfg) for k in repos}
    try:
        return resolve_repo_refs([ref], names)[0]
    except LookupError as e:
        if "ambiguous" in str(e):
            raise
        raise LookupError(f"unknown repository {ref!r} — the configured apt repositories: "
                          + ", ".join(sorted(names.values(), key=str.lower))) from None


def has_archive(repo_key: str) -> bool:
    """Whether `repo_key` could be in an archive mmw knows (its path; the
    Release's Origin is checked by Look)."""
    segment = repo_key.rstrip("/").rsplit("/", 1)[-1]
    return any(segment in a.names for a in pp.ARCHIVES.values())


def _origin(units: list[MirrorConfig], fetch) -> str:
    for unit in units:
        fields = pp.release_fields(fetch, unit.source_url, unit.suite)
        if fields and fields.get("Origin"):
            return fields["Origin"]
    return ""


def look(cfg, mirror_dir, *, as_of: str | None = None, from_url: str | None = None,
         repo: str | None = None, fetch=None) -> list[Target]:
    """Which repositories, which archive, which time — read only. Raises
    ValueError / LookupError (NoArchive, NoMatch, …) with what to say."""
    from mmw.core.repos import repository_label
    fetch = fetch or _fetch
    if bool(as_of) == bool(from_url):
        raise ValueError("give a date (--as-of) or a snapshot URL (--from-url), not both")
    repos = apt_repos(cfg, mirror_dir)
    if not repos:
        raise LookupError("no APT repository is configured")
    key = resolve_repo(cfg, repos, repo) if repo else None
    if from_url:
        archive, name, stamp = pp.parse_archive_url(from_url)
        keys = [key] if key else pp.match_repos(archive, name, stamp, repos, fetch)
    else:
        if key is None:
            raise ValueError("a date needs a repository (--repo NAME)")
        origin = _origin(repos[key], fetch)
        if not origin:
            raise LookupError(f"{repository_label(key, cfg)}'s Release couldn't be read to tell "
                              "which archive holds it")
        archive, name = pp.archive_for(key, origin)
        stamp = pp.stamp_for(archive, as_of)
        keys = [key]
    targets = [Target(archive, name, stamp, k, repository_label(k, cfg),
                      list(dict.fromkeys(u.suite for u in repos[k]))) for k in keys]
    # Automatic URL matching already read the snapshot. Explicit repository
    # selection must check it too, before the TUI enables Add point.
    if key:
        for target in targets:
            releases = [pp.release_fields(fetch, target.base, suite) for suite in target.suites]
            if not any(releases):
                raise pp.NoMatch(f"{target.base}: none of the configured suites "
                                 f"({', '.join(target.suites)}) is available at this time")
    return targets


def report_lines(target: Target, report: pp.PastPointsReport) -> list[str]:
    tail = (f" (from {target.archive.host}, {report.fetched:,} files fetched, "
            f"{report.in_pool:,} already in the pool)")
    lines = [f"Added: {a}{tail}" for a in report.added]
    lines += [f"Already had: {a}" for a in report.already]
    lines += [f"Skipped: {s}" for s in report.skipped]
    lines += [f"⚠ {w}" for w in report.warnings]
    return lines or [f"Nothing recorded for {target.label}."]


@interruptible
def run(cfg_dir: Path, *, as_of: str | None, from_url: str | None, repo: str | None) -> int:
    import typer

    from mmw.core.sync_engine import SyncEvents
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1
    mirror_dir = cfg.settings.mirror_dir
    try:
        targets = look(cfg, mirror_dir, as_of=as_of, from_url=from_url, repo=repo)
    except (ValueError, LookupError) as e:
        eprint(str(e))
        return 1
    code = 0
    for target in targets:
        typer.echo(f"Found: {target.describe()}")
        try:
            report = pp.add_past_points(cfg, mirror_dir, target.repo, target.archive, target.name,
                                        target.stamp, events=SyncEvents(phase=lambda t: typer.echo(f"  {t}")))
        except (pp.PastPointsFailed, LookupError, InterruptedError) as e:
            eprint(f"{target.label}: {e}")
            code = 1
            continue
        for line in report_lines(target, report):
            typer.echo(line)
    return code


@dataclass
class PastRun:
    """A TUI session: each found repository with its plan, fetched together
    on the Sync page and recorded at the end (`record_run`)."""
    runs: list[tuple[Target, pp.PastPlan]]

    @property
    def connections(self) -> int:
        return min(t.archive.connections for t, _p in self.runs)

    @property
    def host(self) -> str:
        return ", ".join(dict.fromkeys(t.archive.host for t, _p in self.runs))

    def mirrors(self) -> list[MirrorConfig]:
        return [u for t, plan in self.runs for u in plan.fetching(t.base)]

    @property
    def skipped(self) -> list[str]:
        return [s for _t, plan in self.runs for s in plan.skipped]


def plan_run(cfg, mirror_dir, targets: list[Target], *, status=None) -> PastRun:
    """Each target's plan, less the suites its archive didn't have then."""
    runs = []
    for t in targets:
        plan = pp.plan_past_points(cfg, mirror_dir, t.repo)
        if plan.units:
            plan = pp.skip_missing_suites(plan, t.base, t.stamp, status=status)
        runs.append((t, plan))
    return PastRun(runs)


def record_run(cfg, run: PastRun, entries: dict, failures: list[str], counts: list[int], *,
               stopped: bool) -> tuple[str, bool]:
    """After the Sync page fetched `run`: record it, or say why not. The
    caller still holds the pool's shared lock. (text, whether it went well)."""
    from mmw.core.pool import Pool
    if stopped:
        return "Stopped — nothing was recorded. Files already downloaded stay in the pool.", False
    if failures:
        return ("Nothing recorded — the archive didn't give everything (a re-run fetches only the "
                "rest):\n" + "\n".join(failures[:5])), False
    lines: list[str] = []
    with Pool(cfg.settings.mirror_dir) as pool:
        for target, plan in run.runs:
            if not plan.units:
                lines += [f"Skipped: {s}" for s in plan.skipped] + [f"⚠ {n}" for n in plan.notes]
                continue
            mine = {u.name: entries[u.name] for u in plan.units if u.name in entries}
            report = pp.record_past_points(cfg, pool, plan, mine)
            report.fetched, report.in_pool = counts
            lines += report_lines(target, report)
    return "\n".join(lines), True
