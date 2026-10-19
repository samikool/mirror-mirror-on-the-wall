"""The requirements list: what must install *together*, per target.

uv resolves the list (backtracking, against the index) to exact pins. A pin
the target can install from a wheel at its floor contributes that release's
wheels; one it can only build from source contributes its sdist and pulls in
the sdist's build requirements, resolved for the same target and followed
the same way until nothing new appears. Files are collected by the same rule
as the straight mirror (`collect_release`).
"""
from collections.abc import Callable
from dataclasses import dataclass, field

from packaging.version import InvalidVersion, Version

from mmw.providers.pypi.closure import KeepSet, Report, collect_release
from mmw.providers.pypi.targets import Floors, TagMatcher, Target, requires_python_ok


@dataclass
class ReqResult:
    keep: KeepSet
    report: Report
    failed: set[str] = field(default_factory=set)            # target keys
    built: list[tuple[str, str]] = field(default_factory=list)   # (target key, "name==version")


def _release_files(universe, name: str, version: str) -> list:
    try:
        want = Version(version)
    except InvalidVersion:
        return []
    out = []
    for f in universe.files(name):
        try:
            if Version(f.version) == want:
                out.append(f)
        except InvalidVersion:
            continue
    return out


def _how(files, target: Target, matcher: TagMatcher):
    """("wheel" | "yanked", None) when a wheel installs at the floor, ("sdist",
    the sdist) when only the source does, (None, None) when neither."""
    for allow_yanked, how in ((False, "wheel"), (True, "yanked")):
        if any(f.kind == "wheel" and (allow_yanked or not f.yanked)
               and requires_python_ok(f.requires_python, target)
               and matcher.priority(f.wheel_tags) is not None for f in files):
            return how, None
    sdist = next((f for f in files if f.kind == "sdist" and not f.yanked
                  and requires_python_ok(f.requires_python, target)), None)
    return ("sdist", sdist) if sdist is not None else (None, None)


def resolve_requirements(lines: list[str], targets: list[Target], floors: Floors, *, sdists: bool,
                         universe, compile_fn: Callable, build_fn: Callable, stop_event=None,
                         progress=None) -> ReqResult:
    say = progress or (lambda _m: None)
    result = ReqResult(KeepSet(), Report())
    report = result.report
    for i, target in enumerate(targets, start=1):
        if stop_event is not None and stop_event.is_set():
            break
        key = target.key
        say(f"{key}: resolving the requirements list ({i}/{len(targets)})")
        compiled = compile_fn(lines, target)
        if compiled.error is not None:
            result.failed.add(key)
            report.unsatisfiable.append((key, "requirements", compiled.error))
            continue
        matcher = TagMatcher(target, floors)
        releases: dict[tuple[str, str], tuple[str, list]] = {}
        queue = list(compiled.pins)
        failed = False
        while queue and not failed:
            name, version = queue.pop(0)
            if (name, version) in releases:
                continue
            files = _release_files(universe, name, version)
            how, sdist = _how(files, target, matcher)
            release = f"{name}=={version}"
            if how is None:
                # Its page failed to load or moved since uv read it: publishing
                # without it would ship a list that no longer installs.
                report.unsatisfiable.append((key, release, "no file in the index installs on this target"))
                result.failed.add(key)
                failed = True
                continue
            releases[(name, version)] = (how, files)
            if how != "sdist":
                continue
            report.built_from_source.append((key, release))
            result.built.append((key, release))
            needs = build_fn(sdist)
            if needs.note:
                report.unknown_deps.append((key, f"{release} build requirements: {needs.note}"))
            if not needs.requires:
                continue
            tools = compile_fn(needs.requires, target)
            if tools.error is not None:
                result.failed.add(key)
                report.unsatisfiable.append((key, f"{release} build requirements", tools.error))
                failed = True
                continue
            queue.extend(pin for pin in tools.pins if pin not in releases)
        if failed:
            continue
        for how, files in releases.values():
            collect_release(result.keep, target, matcher, files, how, sdists)
    report._normalize()
    result.built = sorted(set(result.built))
    return result
