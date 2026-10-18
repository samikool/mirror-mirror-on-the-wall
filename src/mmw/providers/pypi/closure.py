"""The straight mirror's keep policy: latest + dependency closure, per target.

Seed each project's newest installable release; for every kept release read
the core metadata of the wheel the target would install, and for each
dependency keep the newest installable release that satisfies it; repeat
until nothing new is reached. Whatever cannot be guaranteed is reported,
never dropped silently. One function, so a later policy ("eras") can add
seeds without a rewrite.
"""
import json
from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Protocol

from packaging.markers import InvalidMarker, UndefinedComparison, UndefinedEnvironmentName
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from mmw.providers.pypi.index import DistFile
from mmw.providers.pypi.targets import (Floors, TagMatcher, Target, is_universal, marker_env,
                                        requires_python_ok)

UNIVERSAL = "universal"


class Universe(Protocol):
    def names(self) -> Iterable[str]: ...
    def files(self, name: str) -> list[DistFile]: ...
    def scan(self) -> Iterable[tuple[str, list[DistFile]]]: ...   # every project, storage order
    def prefetch(self, files: list[DistFile]) -> None: ...
    def metadata(self, file: DistFile) -> tuple[list[str], list[str]] | None: ...


@dataclass
class Report:
    unsatisfiable: list[tuple[str, str, str]] = field(default_factory=list)  # (target, "n==v", requirement)
    unknown_deps: list[tuple[str, str]] = field(default_factory=list)        # (target, "n==v")
    sdist_fallback: list[tuple[str, str]] = field(default_factory=list)      # (target, "n==v")
    extras_gaps: list[tuple[str, str, str]] = field(default_factory=list)    # (target, "n==v", extra)
    invalid: list[tuple[str, str]] = field(default_factory=list)             # ("n==v", raw line)
    built_from_source: list[tuple[str, str]] = field(default_factory=list)   # (target, "n==v")

    _TARGETED = ("unsatisfiable", "unknown_deps", "sdist_fallback", "extras_gaps", "built_from_source")

    def _normalize(self) -> "Report":
        for name in (*self._TARGETED, "invalid"):
            setattr(self, name, sorted(set(getattr(self, name))))
        return self

    def for_units(self, keys: set[str]) -> "Report":
        return Report(**{name: [r for r in getattr(self, name) if r[0] in keys] for name in self._TARGETED},
                      invalid=list(self.invalid))

    def to_json(self) -> bytes:
        return json.dumps({name: [list(r) for r in getattr(self, name)]
                           for name in (*self._TARGETED, "invalid")},
                          sort_keys=True, separators=(",", ":")).encode()

    def summary(self) -> str:
        return (f"{len(self.unsatisfiable)} unsatisfiable, {len(self.unknown_deps)} unknown dependencies, "
                f"{len(self.sdist_fallback)} sdist fallbacks, {len(self.extras_gaps)} extras gaps, "
                f"{len(self.invalid)} invalid requirements, {len(self.built_from_source)} built from source"
                + (" (build requirements: static only)" if self.built_from_source else ""))


@dataclass
class KeepSet:
    files: dict[str, dict[str, DistFile]] = field(default_factory=dict)  # unit key -> filename -> file

    def add(self, unit: str, f: DistFile) -> None:
        self.files.setdefault(unit, {})[f.filename] = f


@lru_cache(maxsize=1 << 18)
def _ver(text: str) -> Version | None:
    try:
        return Version(text)
    except InvalidVersion:
        return None


class _TargetClosure:
    """One target's fixpoint. Project files are re-read from the universe on
    demand rather than held: at full PyPI scale only small per-project answers
    are cached."""

    def __init__(self, universe: Universe, target: Target, floors: Floors, sdists: bool,
                 report: Report, keep: KeepSet, requirements: dict, stop_event=None, progress=None):
        self.u, self.t, self.sdists = universe, target, sdists
        self.stop_event, self.say = stop_event, progress or (lambda _m: None)
        self.report, self.keep, self._requirements = report, keep, requirements
        self.matcher = TagMatcher(target, floors)
        self.env = marker_env(target)
        self._installable: dict[str, list[Version]] = {}
        # Recently read projects' release tables (bounded), and the files of
        # every release reached — so a project is read about once per target.
        self._tables: OrderedDict[str, dict[Version, list[DistFile]]] = OrderedDict()
        self._release_files: dict[tuple[str, Version], list[DistFile]] = {}
        self._picks: dict[tuple[str, str], tuple[Version, str] | None] = {}
        self._markers: dict[tuple[str, frozenset], bool] = {}

    # ── per-project answers ───────────────────────────────────────────────

    def _usable(self, f: DistFile, *, allow_yanked: bool = False) -> bool:
        return (allow_yanked or not f.yanked) and requires_python_ok(f.requires_python, self.t)

    TABLES = 256

    def _releases(self, name: str, files: list[DistFile] | None = None) -> dict[Version, list[DistFile]]:
        table = self._tables.get(name)
        if table is not None:
            self._tables.move_to_end(name)
            return table
        table = {}
        for f in self.u.files(name) if files is None else files:
            v = _ver(f.version)
            if v is not None:
                table.setdefault(v, []).append(f)
        self._tables[name] = table
        if len(self._tables) > self.TABLES:
            self._tables.popitem(last=False)
        return table

    def _files_of(self, name: str, version: Version) -> list[DistFile]:
        key = (name, version)
        if key not in self._release_files:
            self._release_files[key] = self._releases(name).get(version, [])
        return self._release_files[key]

    def _installable_release(self, files: list[DistFile], *, allow_yanked: bool = False) -> bool:
        return any(self._usable(f, allow_yanked=allow_yanked) and (
            (f.kind == "wheel" and self.matcher.priority(f.wheel_tags) is not None)
            or (f.kind == "sdist" and self.sdists)) for f in files)

    def installable(self, name: str) -> list[Version]:
        if name not in self._installable:
            self._installable[name] = sorted((v for v, fs in self._releases(name).items()
                                              if self._installable_release(fs)), reverse=True)
        return self._installable[name]

    def seed(self, name: str, files: list[DistFile] | None = None) -> Version | None:
        by_v = self._releases(name, files)
        versions = sorted((v for v, fs in by_v.items() if self._installable_release(fs)), reverse=True)
        stable = [v for v in versions if not v.is_prerelease]
        seed = (stable or versions or [None])[0]
        if seed is not None:
            self._release_files[(name, seed)] = by_v[seed]
        return seed

    def chosen_wheel(self, name: str, version: Version, *, allow_yanked: bool = False) -> DistFile | None:
        best, best_p = None, None
        for f in self._files_of(name, version):
            if f.kind != "wheel" or not self._usable(f, allow_yanked=allow_yanked):
                continue
            p = self.matcher.priority(f.wheel_tags)
            if p is not None and (best_p is None or p < best_p):
                best, best_p = f, p
        return best

    def pick(self, req: Requirement) -> tuple[Version, str] | None:
        """(version, how) for a requirement — how is "wheel", "sdist" (the
        wheels-only fallback) or "yanked" (an exact pin to a yanked release)."""
        name = canonicalize_name(req.name)
        key = (name, str(req.specifier))
        if key in self._picks:
            return self._picks[key]
        found = next(iter(req.specifier.filter(self.installable(name))), None)
        result = (found, "wheel") if found is not None else None
        if result is None:
            by_v = self._releases(name)
            if not self.sdists:
                with_sdist = sorted((v for v, fs in by_v.items()
                                     if any(f.kind == "sdist" and self._usable(f) for f in fs)), reverse=True)
                found = next(iter(req.specifier.filter(with_sdist)), None)
                result = (found, "sdist") if found is not None else None
            if result is None and any(s.operator in ("==", "===") for s in req.specifier):
                yanked = sorted((v for v, fs in by_v.items()
                                 if self._installable_release(fs, allow_yanked=True)), reverse=True)
                found = next(iter(req.specifier.filter(yanked)), None)
                result = (found, "yanked") if found is not None else None
        self._picks[key] = result
        return result

    # ── requirements ──────────────────────────────────────────────────────

    def parse(self, release: str, line: str) -> Requirement | None:
        if line not in self._requirements:
            try:
                self._requirements[line] = Requirement(line)
            except InvalidRequirement:
                self._requirements[line] = None
        req = self._requirements[line]
        if req is None:
            self.report.invalid.append((release, line))
        return req

    def active(self, req: Requirement, extras: frozenset) -> bool:
        if req.marker is None:
            return True
        key = (str(req.marker), extras)
        if key not in self._markers:
            try:
                self._markers[key] = any(req.marker.evaluate({**self.env, "extra": x}) for x in ("", *extras))
            except (InvalidMarker, UndefinedComparison, UndefinedEnvironmentName):
                self._markers[key] = False
        return self._markers[key]

    # ── the fixpoint ──────────────────────────────────────────────────────

    def run(self, seeds: Iterable[tuple[str, list[DistFile] | None]]) -> None:
        """`seeds`: (name, its files) — files None to look the project up."""
        key = self.t.key
        kept: dict[tuple[str, Version], str] = {}      # release -> how it was reached
        frontier = []
        for name, files in seeds:
            v = self.seed(name, files)
            if v is not None:
                frontier.append((name, v, frozenset(), "wheel"))
        seed_releases = {(n, v) for n, v, _, _ in frontier}
        seed_meta: dict[tuple[str, Version], tuple[list[str], list[str]]] = {}
        seen: set[tuple[str, Version, frozenset]] = set()
        waves = 0
        while frontier:
            if self.stop_event is not None and self.stop_event.is_set():
                return
            wave = [node for node in dict.fromkeys(frontier) if node[:3] not in seen]
            waves += 1
            self.say(f"{key}: wave {waves}, {len(wave):,} releases")
            seen.update(node[:3] for node in wave)
            chosen: dict[tuple[str, Version], DistFile | None] = {}
            for name, v, _, how in wave:
                if kept.get((name, v)) in (None, "sdist"):     # a wheel reach outranks a fallback
                    kept[(name, v)] = how
                if how != "sdist" and (name, v) not in chosen:
                    chosen[(name, v)] = self.chosen_wheel(name, v, allow_yanked=how == "yanked")
            self.u.prefetch([f for f in chosen.values() if f is not None])
            frontier = []
            for name, v, extras, how in wave:
                release = f"{name}=={v}"
                if how == "sdist":
                    self.report.sdist_fallback.append((key, release))
                    self.report.unknown_deps.append((key, release))
                    continue
                wheel = chosen.get((name, v))
                meta = self.u.metadata(wheel) if wheel is not None else None
                if meta is None:
                    self.report.unknown_deps.append((key, release))
                    continue
                requires, provides = meta
                if provides and (name, v) in seed_releases and not extras:   # only these can have gaps
                    seed_meta[(name, v)] = (requires, provides)
                for line in requires:
                    req = self.parse(release, line)
                    if req is None or not self.active(req, extras):
                        continue
                    if canonicalize_name(req.name) == name:
                        # `pkg[x]; extra == "all"` means *this* release's x, not the newest pkg's.
                        frontier.append((name, v, extras | {canonicalize_name(x) for x in req.extras}, how))
                        continue
                    picked = self.pick(req)
                    if picked is None:
                        self.report.unsatisfiable.append((key, release, line))
                        continue
                    frontier.append((canonicalize_name(req.name), picked[0],
                                     frozenset(canonicalize_name(x) for x in req.extras), picked[1]))
        self._extras_gaps(seed_meta, kept)
        self._collect(kept)

    def _extras_gaps(self, seed_meta, kept) -> None:
        kept_versions: dict[str, list[Version]] = {}
        for name, v in kept:
            kept_versions.setdefault(name, []).append(v)
        for (name, v), (requires, provides) in seed_meta.items():
            for extra in provides:
                extra_key = frozenset({canonicalize_name(extra)})
                for line in requires:
                    req = self._requirements.get(line)
                    if req is None or self.active(req, frozenset()) or not self.active(req, extra_key):
                        continue
                    have = kept_versions.get(canonicalize_name(req.name), [])
                    if not any(req.specifier.contains(h, prereleases=True) for h in have):
                        self.report.extras_gaps.append((self.t.key, f"{name}=={v}", extra))
                        break

    def _collect(self, kept: dict[tuple[str, Version], str]) -> None:
        for (name, v), how in kept.items():
            collect_release(self.keep, self.t, self.matcher, self._files_of(name, v), how, self.sdists)


def collect_release(keep: KeepSet, target: Target, matcher: TagMatcher, files: list[DistFile],
                    how: str, sdists: bool) -> None:
    """The files a kept release contributes on `target` — the one rule both
    flows use. `how`: "wheel", "sdist" (installed from source) or "yanked"
    (an exact pin to a yanked release)."""
    for f in files:
        if (f.yanked and how != "yanked") or not requires_python_ok(f.requires_python, target):
            continue
        if f.kind == "wheel" and matcher.keeps(f.wheel_tags):
            keep.add(UNIVERSAL if is_universal(f.wheel_tags) else target.key, f)
        elif f.kind == "sdist" and (sdists or how == "sdist"):
            keep.add(UNIVERSAL, f)


def keep_closure(universe: Universe, targets: list[Target], floors: Floors, *, sdists: bool,
                 seeds: Iterable[str] | None = None, stop_event=None,
                 progress=None) -> tuple[KeepSet, Report]:
    """Latest + dependency closure for every target. `seeds=None` seeds every
    project in the universe. A set `stop_event` ends it between waves; the
    result is then partial and the caller must not use it."""
    say = progress or (lambda _m: None)
    keep, report = KeepSet(), Report()
    requirements: dict[str, Requirement | None] = {}
    named = None if seeds is None else [canonicalize_name(s) for s in seeds]
    for i, target in enumerate(targets, start=1):
        if stop_event is not None and stop_event.is_set():
            break
        say(f"{target.key}: resolving latest + dependencies ({i}/{len(targets)})")
        _TargetClosure(universe, target, floors, sdists, report, keep, requirements,
                       stop_event, say).run(universe.scan() if named is None else ((n, None) for n in named))
    return keep, report._normalize()
