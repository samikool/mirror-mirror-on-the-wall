"""Apt's "empty means all": fill open suites/components/arches by listing
the repository. Moved out of core/resolve.py so core resolution is provider
dispatch only; behaviour is unchanged (tests/test_resolve.py)."""
from collections.abc import Callable

from mmw.core import discover
from mmw.core.config import MirrorConfig, SourceConfig, SuiteOverride, arch_from_url
from mmw.core.resolve import ResolvedSource, _dimension_notes, _preview


def resolve_apt_flat(src: SourceConfig, *, progress: Callable[[str], None] | None = None) -> ResolvedSource:
    """apt-flat: nothing to enumerate; the arch comes from the URL."""
    def say(msg: str) -> None:
        if progress:
            progress(msg)

    resolved = src.model_copy(deep=True)
    notes: list[str] = []
    errors: list[str] = []

    # apt-flat has no dists/ tree: nothing to enumerate. The architecture is
    # baked into the URL, so derive it from there — defaulting to amd64 here
    # labelled NVIDIA's arm64 `sbsa` and `hpc-sdk` repositories as amd64, and
    # `NetworkSelect` filters on arch, so an arm64 network would have silently
    # dropped them from its published tree.
    #
    # This is the path a sync actually takes. `SourceConfig.effective_arches`
    # has the same rule; fixing only that one left this override in force and
    # the label unchanged after a full re-sync.
    if not resolved.arches:
        arch = arch_from_url(src.url)
        resolved.arches = [arch] if arch else []
        # Reported like every other expansion: a dimension mmw filled in
        # on the user's behalf must never be silent.
        note = f"{src.name}: arch from URL → {arch or 'not stated (unknown)'}"
        notes.append(note)
        say(note)
    return ResolvedSource(resolved, resolved.to_mirrors(), notes, errors)


def resolve_apt(src: SourceConfig, *, progress: Callable[[str], None] | None = None) -> ResolvedSource:
    """Fill a source's empty dimensions by listing the repository.

    A dimension the user *did* specify is never touched — the config states a
    selection, and this only supplies the ones left open.
    """
    def say(msg: str) -> None:
        if progress:
            progress(msg)

    resolved = src.model_copy(deep=True)
    notes: list[str] = []
    errors: list[str] = []

    if not resolved.suites:
        say(f"{src.name}: listing suites…")
        found = discover.list_suites(src.url)
        if not found:
            errors.append(f"{src.name}: no suites found at {src.url}/dists/")
            return ResolvedSource(resolved, [], notes, errors)
        resolved.suites = found
        notes.append(f"{src.name}: all suites → {len(found)} ({_preview(found)})")

    # **Resolve per suite, never as a union.** The old code unioned the
    # components of every suite, then unioned the arches of whichever suite
    # answered first, then let `to_mirrors()` take the cross product of the
    # three flat lists. For a vendor repository whose suites differ — one
    # carrying `common`, another `contrib`; one built for arm64, another for
    # amd64 — that invents suite×component×arch combinations that do not exist
    # upstream. Each invented mirror 404s its index, aborts, and makes
    # `mmw sync --quiet` exit 1 on a repository that is perfectly healthy.
    override_map = {o.suite: list(o.components) for o in resolved.suite_overrides}
    stated_suites = bool(src.suites)
    comps_by_suite: dict[str, list[str]] = {}
    arches_by_suite: dict[str, list[str]] = {}
    comps_via: dict[str, str] = {}
    arches_via: dict[str, str] = {}
    components_resolved = False
    arches_resolved = False
    dropped: list[str] = []

    for suite in list(resolved.suites):
        # `SuiteOverride(components=[])` used to mean "mirror nothing": the
        # override replaced the source-level list, and resolution only ever
        # looked at the source-level list, so an empty override was never
        # filled in and never reported. The user asked for a suite, mmw wrote
        # it down, mirrored nothing and exited 0.
        comps = override_map.get(suite, resolved.components)
        if not comps:
            components_resolved = True
            say(f"{src.name}: listing components in {suite}…")
            comps = discover.list_components(src.url, suite)
            via = "listing"
            if not comps:
                # No directory listing at all — read the Release file instead.
                comps, _ = discover.release_dimensions(src.url, suite)
                via = "Release file"
            comps_via[suite] = via
        if not comps:
            if stated_suites:
                errors.append(f"{src.name}: no components found under suite {suite}")
            dropped.append(suite)
            continue

        arches = resolved.arches
        if not arches:
            arches_resolved = True
            say(f"{src.name}: listing architectures in {suite}…")
            # The arch set does not vary between components of a suite, so the
            # first component that answers settles it.
            for comp in comps:
                arches = discover.list_arches(src.url, suite, comp)
                if arches:
                    break
            via = "listing"
            if not arches:
                # Only reached when listing produced NOTHING: where a listing
                # exists it is authoritative, because Ubuntu's archive and
                # ports hosts both advertise every architecture while each
                # serves a subset.
                _, arches = discover.release_dimensions(src.url, suite)
                via = "Release file"
            arches_via[suite] = via
        if not arches:
            if stated_suites:
                errors.append(
                    f"{src.name}: no binary-* directories listed and no Architectures "
                    f"in {src.url}/dists/{suite}/Release"
                )
            dropped.append(suite)
            continue

        comps_by_suite[suite] = comps
        arches_by_suite[suite] = arches

    if not comps_by_suite:
        if not errors:
            errors.append(f"{src.name}: no components found under any suite")
        return ResolvedSource(resolved, [], notes, errors)
    if errors:
        # A suite the *user named* that resolves to nothing is a failure, not a
        # subset — reporting it is the whole point of resolving out loud.
        return ResolvedSource(resolved, [], notes, errors)
    if dropped:
        notes.append(
            f"{src.name}: {len(dropped)} discovered suite(s) list nothing, skipped "
            f"({_preview(dropped)})"
        )

    resolved.suites = [s for s in resolved.suites if s in comps_by_suite]

    if components_resolved:
        # Expressed the only way `SourceConfig` can express it: a source-level
        # list plus a `SuiteOverride` for each suite that differs.
        first = resolved.suites[0]
        resolved.components = comps_by_suite[first]
        resolved.suite_overrides = [
            SuiteOverride(suite=s, components=comps_by_suite[s])
            for s in resolved.suites[1:]
            if comps_by_suite[s] != resolved.components
        ]
        notes.extend(_dimension_notes(
            src.name, "components", comps_by_suite, comps_via, resolved.suites))

    if arches_resolved:
        # `SourceConfig` has no per-suite arch field, so the flat copy carries
        # the union while `mirrors` below is built per suite — the mirrors are
        # what a sync uses, and they contain no invented combination.
        union: list[str] = []
        for suite in resolved.suites:
            for a in arches_by_suite[suite]:
                if a not in union:
                    union.append(a)
        resolved.arches = union
        notes.extend(_dimension_notes(
            src.name, "arches", arches_by_suite, arches_via, resolved.suites))

    mirrors: list[MirrorConfig] = []
    for suite in resolved.suites:
        per = resolved.model_copy(deep=True)
        per.suites = [suite]
        per.components = comps_by_suite[suite]
        per.arches = arches_by_suite[suite]
        per.suite_overrides = []
        mirrors.extend(per.to_mirrors())

    return ResolvedSource(resolved, mirrors, notes, errors)
