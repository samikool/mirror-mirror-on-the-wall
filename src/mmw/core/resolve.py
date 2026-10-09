"""Turning "all" into concrete suites, components and architectures.

An empty `suites` / `components` / `arches` list on a source means *everything
upstream serves* (see docs/history/repository-first-redesign.md). Resolving that needs the
network, so it cannot live in `SourceConfig.to_mirrors()`, which is a pure
function used offline by publish, certs and the manage listing.

Instead the sync entry points call `resolve_sources()` first and sync the
concrete mirrors it returns. What "all" expanded to is reported before anything
is downloaded — a blank field must never quietly become a hundred mirrors.
"""
from collections.abc import Callable
from dataclasses import dataclass, field

from mmw.core import discover
from mmw.core.config import MirrorConfig, SourceConfig, SuiteOverride, arch_from_url


@dataclass
class ResolvedSource:
    source: SourceConfig          # a copy with every dimension filled in
    mirrors: list[MirrorConfig]
    notes: list[str] = field(default_factory=list)   # what "all" expanded to
    errors: list[str] = field(default_factory=list)  # nothing listed upstream


def resolve_source(
    src: SourceConfig,
    *,
    progress: Callable[[str], None] | None = None,
) -> ResolvedSource:
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

    # apt-flat has no dists/ tree: nothing to enumerate. The architecture is
    # baked into the URL, so derive it from there — defaulting to amd64 here
    # labelled NVIDIA's arm64 `sbsa` and `hpc-sdk` repositories as amd64, and
    # `NetworkSelect` filters on arch, so an arm64 network would have silently
    # dropped them from its published tree.
    #
    # This is the path a sync actually takes. `SourceConfig.effective_arches`
    # has the same rule; fixing only that one left this override in force and
    # the label unchanged after a full re-sync.
    if src.provider == "apt-flat":
        if not resolved.arches:
            arch = arch_from_url(src.url)
            resolved.arches = [arch] if arch else []
            # Reported like every other expansion: a dimension mmw filled in
            # on the user's behalf must never be silent.
            note = f"{src.name}: arch from URL → {arch or 'not stated (unknown)'}"
            notes.append(note)
            say(note)
        return ResolvedSource(resolved, resolved.to_mirrors(), notes, errors)

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


def _dimension_notes(
    name: str,
    dimension: str,
    by_suite: dict[str, list[str]],
    via: dict[str, str],
    suites: list[str],
) -> list[str]:
    """What "all" expanded to — one line when every suite agrees, one line per
    distinct answer when they do not. A repository with 55 suites must not
    print 55 identical lines, and one whose suites genuinely differ must not
    hide it behind a union."""
    first = by_suite[suites[0]]
    source = via.get(suites[0], "listing")
    if all(by_suite[s] == first for s in suites):
        return [f"{name}: all {dimension} → {_preview(first)} [{source}]"]
    lines = [f"{name}: all {dimension} → {_preview(first)} [{source}] ({suites[0]})"]
    seen = {tuple(first)}
    for s in suites[1:]:
        if tuple(by_suite[s]) in seen:
            continue
        seen.add(tuple(by_suite[s]))
        lines.append(
            f"{name}: {s} {dimension} → {_preview(by_suite[s])} "
            f"[{via.get(s, 'listing')}]"
        )
    return lines


def resolve_sources(
    sources: list[SourceConfig],
    *,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[MirrorConfig], list[str], list[str]]:
    """Resolve every source. Returns (mirrors, notes, errors).

    A source that cannot be listed contributes no mirrors and one error — it is
    reported, never silently skipped.
    """
    mirrors: list[MirrorConfig] = []
    notes: list[str] = []
    errors: list[str] = []
    for src in sources:
        r = resolve_source(src, progress=progress)
        mirrors.extend(r.mirrors)
        notes.extend(r.notes)
        errors.extend(r.errors)
    return mirrors, notes, errors


def needs_resolving(sources: list[SourceConfig]) -> bool:
    """True if any source leaves a dimension open (so listing is required)."""
    return any(
        src.provider != "apt-flat"
        and (not src.suites or not src.components or not src.arches)
        for src in sources
    )


def _preview(items: list[str], limit: int = 6) -> str:
    head = ", ".join(items[:limit])
    return head + ("…" if len(items) > limit else "")


def pooled_mirrors(cfg, mirror_dir) -> list[MirrorConfig]:
    """What the pool actually holds, restricted to repositories still in the
    config. **Empty when nothing has been synced** — the caller decides what to
    do about that.

    Separate from `available_mirrors` because "the pool knows this repository"
    and "here is a usable list" are different questions. The manage list needs
    the first: config expansion produces *nothing* for an open `arches = []` or
    `components = []` — never invent an architecture — so it must be able to
    tell a real answer from an unresolved one.
    """
    from mmw.core.pool import Pool

    configured = sorted({s.identity for s in cfg.sources})
    try:
        with Pool(mirror_dir, read_only=True) as pool:
            return pool.mirrors(configured)
    except Exception:
        return []


def available_mirrors(cfg, mirror_dir, pooled: "list[MirrorConfig] | None" = None) -> list[MirrorConfig]:
    """The mirrors that actually exist, for publish / certs / network editing.

    Read from the **pool**, not from config expansion: architectures are
    discovered rather than declared, so `cfg.mirrors` only lists the ones the
    config happens to spell out and would hide every other arch of a repository
    mirroring "all" of them.

    Restricted to repositories still in the config, so removing a source stops
    offering its mirrors. Falls back to config expansion when the pool has no
    record yet — nothing has been synced, and an empty list would be a worse
    answer than an optimistic one.
    """
    # `pooled` lets a caller that has already paid for the pool query reuse it.
    # Pool queries are not cheap on a real pool, and asking twice for one screen
    # refresh is a stall the user can feel.
    if pooled is None:
        pooled = pooled_mirrors(cfg, mirror_dir)
    return pooled or cfg.mirrors


def network_mirrors(network, cfg, mirror_dir) -> list[MirrorConfig]:
    """The mirrors a network publishes.

    `network.select` filters what the pool actually holds, by dimension. An
    empty `select` means every mirrored repository; an empty dimension inside a
    select means all of that dimension.

    Selecting by dimension rather than by a frozen list of mirror names is what
    makes "this network is amd64" survive a new architecture appearing upstream
    — the old name list silently stopped publishing anything it did not already
    name, and said nothing about it.
    """
    available = available_mirrors(cfg, mirror_dir)
    if not network.select:
        return available
    return [m for m in available if any(sel.matches(m) for sel in network.select)]


def network_mirror_names(network, cfg, mirror_dir) -> list[str]:
    return [m.name for m in network_mirrors(network, cfg, mirror_dir)]
