"""Turning "all" into concrete suites, components and architectures.

An empty `suites` / `components` / `arches` list on a source means *everything
upstream serves* (see repository-first-redesign.md). Resolving that needs the
network, so it cannot live in `SourceConfig.to_mirrors()`, which is a pure
function used offline by publish, certs and the manage listing.

Instead the sync entry points call `resolve_sources()` first and sync the
concrete mirrors it returns. What "all" expanded to is reported before anything
is downloaded — a blank field must never quietly become a hundred mirrors.
"""
from collections.abc import Callable
from dataclasses import dataclass, field

from mmw.core import discover
from mmw.core.config import MirrorConfig, SourceConfig, arch_from_url


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

    if not resolved.components:
        say(f"{src.name}: listing components…")
        components: list[str] = []
        for suite in resolved.suites:
            for c in discover.list_components(src.url, suite):
                if c not in components:
                    components.append(c)
        via = "listing"
        if not components:
            # No directory listing at all — read the Release file instead.
            components, _ = discover.release_dimensions(src.url, resolved.suites[0])
            via = "Release file"
        if not components:
            errors.append(f"{src.name}: no components found under any suite")
            return ResolvedSource(resolved, [], notes, errors)
        resolved.components = components
        notes.append(f"{src.name}: all components → {_preview(components)} [{via}]")

    if not resolved.arches:
        say(f"{src.name}: listing architectures…")
        arches: list[str] = []
        # The arch set does not vary between components of a suite, so the first
        # component that answers settles it.
        for suite in resolved.suites:
            for comp in resolved.components:
                found = discover.list_arches(src.url, suite, comp)
                if found:
                    arches = found
                    break
            if arches:
                break
        via = "listing"
        if not arches:
            # Nothing listable — fall back to the Release file's Architectures.
            # Only reached when listing produced NOTHING: where a listing exists
            # it is authoritative, because Ubuntu's archive and ports hosts both
            # advertise every architecture while each serves a subset.
            _, arches = discover.release_dimensions(src.url, resolved.suites[0])
            via = "Release file"
        if not arches:
            errors.append(
                f"{src.name}: no binary-* directories listed and no Architectures "
                f"in {src.url}/dists/{resolved.suites[0]}/Release"
            )
            return ResolvedSource(resolved, [], notes, errors)
        resolved.arches = arches
        notes.append(f"{src.name}: all arches → {_preview(arches)} [{via}]")

    return ResolvedSource(resolved, resolved.to_mirrors(), notes, errors)


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
    the first: config expansion invents an `amd64` for `arches = []` and
    produces *nothing* for `components = []`, so it must be able to tell a real
    answer from a placeholder.
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
