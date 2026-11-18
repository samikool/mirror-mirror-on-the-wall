"""Past points: a Debian or Ubuntu repository's state at a past time, from
the distribution's snapshot archive (snapshot.debian.org, snapshot.ubuntu.com).

This module knows the archives (a table — others can be added), reads which
archive and time a URL or a date means, and works out which configured
repositories a snapshot is a copy of.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

from mmw.core.apt_index import release_body
from mmw.core.config import MirrorConfig
from mmw.core.points import resolve_when

_STAMP = re.compile(r"^\d{8}T\d{6}Z$")
_COMPARED = ("Origin", "Label", "Codename")


class NoArchive(LookupError):
    def __init__(self, repo: str):
        self.repo = repo
        super().__init__(
            f"{repo} has no snapshot archive — past points come from "
            + " and ".join(f"{a.host} ({', '.join(a.origins)})" for a in ARCHIVES.values()))


class ArchiveUnavailable(LookupError):
    pass


def archive_http_error(url: str, code: int) -> str:
    message = f"{url}: HTTP {code}"
    if urlparse(url).hostname == "snapshot.ubuntu.com" and code in (401, 403):
        message += (" — Ubuntu's public snapshot service refused access. "
                    "Its documented /ubuntu/TIMESTAMP/ URLs require no credentials; "
                    "check https://snapshot.ubuntu.com/ and try again later. "
                    "This response doesn't mean the suite is missing.")
    return message


def fetch_release(url: str) -> bytes | None:
    """Read metadata without treating access or transport failures as absence."""
    import httpx

    from mmw.core.http import retrying_client
    try:
        with retrying_client(follow_redirects=True, timeout=30) as client:
            response = client.get(url)
    except httpx.HTTPError as e:
        raise ArchiveUnavailable(f"{url}: {e}") from e
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise ArchiveUnavailable(archive_http_error(url, response.status_code))
    return response.content


class BeforeArchive(ValueError):
    def __init__(self, stamp: str, start: str):
        self.stamp, self.start = stamp, start
        super().__init__(f"{stamp} is too early — the archive starts {start[:4]}-{start[4:6]}-{start[6:8]}")


class NoMatch(LookupError):
    pass


@dataclass(frozen=True)
class Archive:
    key: str
    host: str
    prefix: str                     # the path before the archive name ("/archive" or "")
    names: tuple[str, ...]          # archive names, matched to a repository's last path segment
    origins: tuple[str, ...]        # Release Origins it holds
    start: str = ""                 # the first stamp it has, if it says
    connections: int = 4            # be gentle: these are shared public services
    unavailable: tuple[tuple[str, str], ...] = ()   # (name, why not)

    def base(self, name: str, stamp: str) -> str:
        return f"https://{self.host}{self.prefix}/{name}/{stamp}"

    def check(self, name: str) -> None:
        why = dict(self.unavailable).get(name)
        if why:
            raise ArchiveUnavailable(why)


_UBUNTU_PORTS = "Ubuntu's ports archive (arm64, armhf, ppc64el, riscv64, s390x) isn't public on snapshot.ubuntu.com"

ARCHIVES: dict[str, Archive] = {
    "debian": Archive(
        key="debian", host="snapshot.debian.org", prefix="/archive",
        names=("debian", "debian-security", "debian-ports", "debian-debug", "debian-backports",
               "debian-archive", "debian-volatile"),
        origins=("Debian", "Debian Backports"), connections=2),
    "ubuntu": Archive(
        key="ubuntu", host="snapshot.ubuntu.com", prefix="",
        names=("ubuntu", "ubuntu-ports"), origins=("Ubuntu",), start="20230301T000000Z",
        unavailable=(("ubuntu-ports", _UBUNTU_PORTS),)),
}


def stamp_for(archive: Archive, text: str) -> str:
    """A date or stamp as the full stamp the archive is asked for."""
    try:
        stamp = text if _STAMP.match(text) else resolve_when(text)
    except ValueError as e:
        raise ValueError(f"{text!r} isn't a snapshot time (YYYYMMDDTHHMMSSZ or a day)") from e
    if archive.start and stamp < archive.start:
        raise BeforeArchive(stamp, archive.start)
    return stamp


def parse_archive_url(url: str) -> tuple[Archive, str, str]:
    """(archive, name, stamp) for a snapshot URL; anything below the stamp is
    ignored. A bare day means its end, always given back as a full stamp."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    archive = next((a for a in ARCHIVES.values() if a.host == host), None)
    if archive is None:
        raise ValueError(f"{url} isn't a snapshot archive URL — mmw knows "
                         + " and ".join(a.host for a in ARCHIVES.values()))
    path = parsed.path
    if archive.prefix:
        if not path.startswith(archive.prefix + "/"):
            raise ValueError(f"{url} isn't a snapshot URL: expected https://{archive.host}{archive.prefix}/NAME/TIME/")
        path = path[len(archive.prefix):]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"{url} names no time: expected https://{archive.host}{archive.prefix}/NAME/TIME/")
    name, when = parts[0], parts[1]
    if name not in archive.names:
        raise ValueError(f"{url}: {archive.host} has no archive {name!r} that mmw knows "
                         f"({', '.join(archive.names)})")
    archive.check(name)
    return archive, name, stamp_for(archive, when)


def _last_segment(repo: str) -> str:
    return urlparse(repo).path.rstrip("/").rsplit("/", 1)[-1]


def archive_for(repo_key: str, origin: str) -> tuple[Archive, str]:
    """The archive and archive name holding `repo_key`, from its Release
    Origin and the last segment of its path (`/debian-security`)."""
    name = _last_segment(repo_key)
    for archive in ARCHIVES.values():
        if origin in archive.origins and name in archive.names:
            archive.check(name)
            return archive, name
    raise NoArchive(repo_key)


def _fields(data: bytes | None) -> dict[str, str] | None:
    if not data:
        return None
    text = (release_body(data) if data.lstrip().startswith(b"-----BEGIN PGP") else data)
    out: dict[str, str] = {}
    for ln in text.decode("utf-8", errors="replace").splitlines():
        if ln and not ln[0].isspace() and ":" in ln:
            key, value = ln.split(":", 1)
            if key in _COMPARED:
                out[key] = value.strip()
    return out


def release_fields(fetch: Callable[[str], bytes | None], base: str, suite: str) -> dict[str, str] | None:
    for name in ("Release", "InRelease"):
        try:
            data = fetch(f"{base.rstrip('/')}/dists/{suite}/{name}")
        except ArchiveUnavailable:
            raise
        except Exception:  # noqa: BLE001 — unreadable is "not there" here
            data = None
        if data:
            return _fields(data)
    return None


def _say(fields: dict[str, str]) -> str:
    return ", ".join(f"{k} {fields[k]}" for k in _COMPARED if k in fields) or "no fields"


def match_repos(archive: Archive, name: str, stamp: str, candidates: dict[str, list[MirrorConfig]],
                fetch: Callable[[str], bytes | None], *,
                own: Callable[[MirrorConfig], bytes | None] | None = None) -> list[str]:
    """The candidate repositories this snapshot is a copy of, sorted.

    Candidates are narrowed first by path (the last segment equals the
    archive name; apt repositories only). Then each one's suites are read
    from the snapshot and compared with its own Release (`own`, else fetched
    from its source URL) on Origin, Label and Codename. A repository matches
    when one suite agrees and none disagrees — a suite the snapshot lacks
    then is no disagreement. None matching raises NoMatch saying what was
    compared."""
    archive.check(name)
    base = archive.base(name, stamp)
    found: list[str] = []
    compared: list[str] = []
    for repo, units in sorted(candidates.items()):
        if _last_segment(repo) != name:
            continue
        units = [u for u in units if u.provider == "apt" and u.suite]
        if not units:
            continue
        agreed, disagreed = False, False
        for suite in dict.fromkeys(u.suite for u in units):
            unit = next(u for u in units if u.suite == suite)
            theirs = release_fields(fetch, base, suite)
            if theirs is None:
                compared.append(f"{repo} {suite}: not in the snapshot")
                continue
            mine = _fields(own(unit)) if own is not None else release_fields(fetch, unit.source_url, suite)
            if mine is None:
                compared.append(f"{repo} {suite}: its own Release couldn't be read")
                continue
            if all(mine.get(k) == theirs.get(k) for k in _COMPARED):
                agreed = True
            else:
                disagreed = True
                compared.append(f"{repo} {suite}: snapshot has {_say(theirs)}; the repository has {_say(mine)}")
        if agreed and not disagreed:
            found.append(repo)
    if not found:
        where = f"{archive.host}{archive.prefix}/{name} at {stamp}"
        if not compared:
            raise NoMatch(f"no configured apt repository ends in /{name}, so none can be a copy of {where}")
        raise NoMatch(f"no configured repository matches {where} (compared Origin, Label, Codename):\n  "
                      + "\n  ".join(compared))
    return found



# ── recording a past state ───────────────────────────────────────────────────

class PastPointsFailed(RuntimeError):
    """The archive couldn't give the whole state: nothing was recorded. Files
    already downloaded stay in the pool, so a re-run fetches only the rest."""


@dataclass
class PastPointsReport:
    added: list[str] = field(default_factory=list)      # "Debian bookworm 2026-10-14": new points
    already: list[str] = field(default_factory=list)    # the same Release and files were a point
    skipped: list[str] = field(default_factory=list)    # networks named, and why
    fetched: int = 0                                    # files downloaded from the archive
    in_pool: int = 0                                    # files the pool already had
    warnings: list[str] = field(default_factory=list)


@dataclass
class PastPlan:
    """All configured units of `repo` to fetch, and networks to link without layers."""
    repo: str
    label: str
    networks: list[tuple[object, list[MirrorConfig]]]
    units: list[MirrorConfig]
    skipped: list[str]
    notes: list[str] = field(default_factory=list)     # warnings the report carries (suites skipped)

    def fetching(self, base: str) -> list[MirrorConfig]:
        """The units, each fetching from `base` instead of its own URL."""
        return [u.model_copy(update={"fetch_base": base}) for u in self.units]

    def without_suites(self, gone: list[str], note) -> "PastPlan":
        """This plan less the units of `gone` suites, each said by `note(suite)`;
        a network left with none drops out."""
        if not gone:
            return self
        keep = lambda units: [u for u in units if u.suite not in gone]  # noqa: E731
        networks = [(n, keep(units)) for n, units in self.networks if keep(units)]
        return PastPlan(self.repo, self.label, networks, keep(self.units), list(self.skipped),
                        [*self.notes, *(note(s) for s in gone)])


def _status(url: str) -> int:
    from mmw.core.http import retrying_client
    with retrying_client(follow_redirects=True, timeout=30) as client:
        return client.get(url).status_code


def skip_missing_suites(plan: PastPlan, base: str, stamp: str, *, status=None) -> PastPlan:
    """Drop the suites the archive didn't have at `stamp` (its Release is a
    404 — a pocket created later), each with a warning. Anything else that
    isn't a Release (a 429, a 5xx, no answer) raises PastPointsFailed: only
    "not there" is a reason to go on without it."""
    from mmw.site.versionref import day
    status = status or _status
    gone = []
    for suite in dict.fromkeys(u.suite for u in plan.units):
        found = None
        for name in ("InRelease", "Release"):
            url = f"{base.rstrip('/')}/dists/{suite}/{name}"
            try:
                code = status(url)
            except Exception as e:  # noqa: BLE001 — named, nothing recorded
                raise PastPointsFailed(f"{url}: {e}") from e
            if code == 200:
                found = True
                break
            if code != 404:
                raise PastPointsFailed(archive_http_error(url, code))
        if not found:
            gone.append(suite)
    return plan.without_suites(gone, lambda s: f"{s}: not in the archive on {day(stamp)} — skipped")


def plan_past_points(cfg, mirror_dir, repo_key: str) -> PastPlan:
    from mmw.core.repos import repository_key, repository_label
    from mmw.core.resolve import network_mirrors
    label = repository_label(repo_key, cfg)
    networks, skipped = [], []
    for network in cfg.networks:
        units = [m for m in network_mirrors(network, cfg, mirror_dir)
                 if repository_key(m) == repo_key and m.provider == "apt"]
        if not units:
            continue
        if network.layer_for(repo_key) is not None:
            skipped.append(f"{network.name} adds its own debs to {label} — past points don't "
                           "support layered repositories yet")
            continue
        networks.append((network, units))
    from mmw.core.resolve import available_mirrors
    wanted = {(u.source, tuple(sorted(u.dims.items()))): u
              for u in [*available_mirrors(cfg, mirror_dir), *cfg.mirrors]
              if repository_key(u) == repo_key and u.provider == "apt"}
    if not wanted:
        raise LookupError(f"no configured APT units for {label}")
    return PastPlan(repo_key, label, networks, sorted(wanted.values(), key=lambda u: u.name), skipped)


def _valid_until_passed(release: bytes | None, now) -> bool:
    from datetime import timezone
    from email.utils import parsedate_to_datetime
    if not release:
        return False
    text = (release_body(release) if release.lstrip().startswith(b"-----BEGIN PGP") else release)
    value = next((ln.split(":", 1)[1].strip() for ln in text.decode("utf-8", "replace").splitlines()
                  if ln.startswith("Valid-Until:")), None)
    if value is None:
        return False
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when < now


def fetch_failure(base: str, summary) -> str | None:
    """Why a unit's fetch from the archive can't be recorded, or None."""
    if summary.stopped:
        return "stopped — nothing was recorded"
    problems = [*summary.errors, *summary.metadata_errors, *summary.pool_write_errors]
    if summary.failed and not summary.errors:
        problems.append(f"{summary.failed} file(s) failed")
    if not summary.metadata_ok and not summary.metadata_errors:
        problems.append("the index couldn't be read")
    if not problems:
        return None
    more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
    return f"{base}: " + "; ".join(problems[:5]) + more


class _FetchedRefs:
    """The pool, except that a mirror's files are what the archive gave — so
    `publish_group` lays a past state out exactly as the walk lays out a
    synced one (by-hash copies, deduplication and all)."""

    def __init__(self, pool, entries: dict[str, list[tuple[str, str, str]]]):
        self._pool, self._entries = pool, entries

    def mirror_files(self, mirror_names, kinds=None, prefix=""):
        wanted = {(rp, cs, kind) for n in mirror_names for cs, rp, kind in self._entries.get(n, ())
                  if (kinds is None or kind in kinds) and rp.startswith(prefix)}
        info = self._pool.blob_info(cs for _rp, cs, _k in wanted)
        return sorted((rp, str(info[cs][0]), info[cs][1], kind, cs)
                      for rp, cs, kind in wanted if cs in info)

    def __getattr__(self, name):
        return getattr(self._pool, name)


def record_past_points(cfg, pool, plan: PastPlan, entries: dict[str, list[tuple[str, str, str]]], *,
                       now: str | None = None) -> PastPointsReport:
    """Split what each unit fetched into points, link them to every planned
    network, and say what was added. The caller holds the pool's shared lock
    since before the fetch: until now the files are orphans to a prune.

    Points are dated by their Releases and first seen now; nothing a sync
    owns changes (refs, recordings, the mirror record). A point a network
    already has is not linked again — that would make it look served now."""
    from datetime import datetime, timezone

    from mmw.core import disk_io
    from mmw.core.points import _say
    from mmw.core.publisher.manifest import now_stamp

    now = now or now_stamp()
    report = PastPointsReport(skipped=list(plan.skipped), warnings=list(plan.notes))
    pool.flush_writes()                         # downloaded rows, before they are read back
    when = datetime.now(timezone.utc)
    from mmw.core.repository_points import record_repository, selected_point_files, cache_slice
    fetched = _FetchedRefs(pool, entries)
    for pid, new, draft in record_repository(pool, plan.repo, plan.units, cfg.settings.mirror_dir, now, refs=fetched):
        for network, units in plan.networks:
            files = selected_point_files(pool, pid, units)
            if not files:
                continue
            if pool.network_point_last_seen(network.name, pid) is None:
                pool.link_network_point(network.name, pid, now)
            cache_slice(pool, network.name, pid, files, units)
        said = _say(plan.repo, draft, cfg)
        if new and said not in report.added:
            report.added.append(said)
        elif not new and said not in report.added and said not in report.already:
            report.already.append(said)
        release = next((draft.files[f"{draft.track}/{n}"] for n in ("Release", "InRelease")
                        if f"{draft.track}/{n}" in draft.files), None)
        blob = pool.blob_file(release) if release else None
        if _valid_until_passed(disk_io.ops.read_bytes(blob) if blob is not None else None, when):
            warning = (f"{said.removeprefix(plan.label + ' ')}: Valid-Until passed — clients need "
                       "Check-Valid-Until=false")
            if warning not in report.warnings:
                report.warnings.append(warning)
    return report


def add_past_points(cfg, mirror_dir, repo_key: str, archive: Archive, name: str, stamp: str, *,
                    base: str | None = None, events=None, stop_event=None,
                    now: str | None = None, status=None) -> PastPointsReport:
    """Record `repo_key`'s state in `archive` at `stamp` as repository points,
    linked to selecting networks — fetched with all configured units through
    the sync's own code, every URL taken from the archive (`base`, default
    the archive's), identity the repository's own, so files the pool has are
    never fetched again. Any failure raises PastPointsFailed (or
    InterruptedError on a stop) before anything is recorded."""
    import asyncio
    import concurrent.futures

    from mmw.core.pool import Pool, shared_pool_lock
    from mmw.core.sync_engine import SyncEvents, sync_mirror

    archive.check(name)
    base = (base or archive.base(name, stamp)).rstrip("/")
    plan = plan_past_points(cfg, mirror_dir, repo_key)
    if plan.units:
        plan = skip_missing_suites(plan, base, stamp, status=status)
    if not plan.units:
        return PastPointsReport(skipped=list(plan.skipped), warnings=list(plan.notes))
    entries: dict[str, list[tuple[str, str, str]]] = {}
    fetched = in_pool = 0
    with shared_pool_lock(mirror_dir), Pool(mirror_dir) as pool:
        async def fetch_all(executor):
            nonlocal fetched, in_pool
            for unit in plan.fetching(base):
                try:
                    summary = await sync_mirror(unit, pool=pool, mirror_dir=mirror_dir,
                                                max_connections=archive.connections, executor=executor,
                                                events=events or SyncEvents(), stop_event=stop_event,
                                                commit_refs=False)
                except Exception as e:  # noqa: BLE001 — named, with where it came from
                    raise PastPointsFailed(f"{base}: {unit.suite}: {e}") from e
                if summary.stopped:
                    raise InterruptedError("stopped — nothing was recorded")
                why = fetch_failure(base, summary)
                if why:
                    raise PastPointsFailed(why)
                fetched += summary.downloaded
                in_pool += summary.pooled
                entries[unit.name] = summary.entries

        with concurrent.futures.ThreadPoolExecutor(max_workers=archive.connections) as ex:
            asyncio.run(fetch_all(ex))
        report = record_past_points(cfg, pool, plan, entries, now=now)
    report.fetched, report.in_pool = fetched, in_pool
    return report
