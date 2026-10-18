"""Turning "all" into concrete suites, components and architectures.

An empty `suites` / `components` / `arches` list on a source means *everything
upstream serves*. Resolving that needs the
network, so it cannot live in `SourceConfig.to_mirrors()`, which is a pure
function used offline by publish, certs and the manage listing.

Instead the sync entry points call `resolve_sources()` first and sync the
concrete mirrors it returns. What "all" expanded to is reported before anything
is downloaded — a blank field must never quietly become a hundred mirrors.
"""
from collections.abc import Callable
from dataclasses import dataclass, field

from mmw.core.config import MirrorConfig, SourceConfig


@dataclass
class ResolvedSource:
    source: SourceConfig          # a copy with every dimension filled in
    mirrors: list[MirrorConfig]
    notes: list[str] = field(default_factory=list)   # what "all" expanded to
    errors: list[str] = field(default_factory=list)  # nothing listed upstream


def resolve_source(src: SourceConfig, *, progress: Callable[[str], None] | None = None,
                   context=None) -> ResolvedSource:
    from mmw.providers import get_provider
    return get_provider(src.provider).resolve(src, progress=progress, context=context)


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
    context=None,
) -> tuple[list[MirrorConfig], list[str], list[str]]:
    """Resolve every source. Returns (mirrors, notes, errors).

    A source that cannot be listed contributes no mirrors and one error — it is
    reported, never silently skipped.
    """
    mirrors: list[MirrorConfig] = []
    notes: list[str] = []
    errors: list[str] = []
    for src in sources:
        try:
            r = resolve_source(src, progress=progress, context=context)
        except Exception as e:  # noqa: BLE001 — one source must not sink the rest
            errors.append(f"{src.name}: could not be resolved — {type(e).__name__}: {e}")
            continue
        mirrors.extend(r.mirrors)
        notes.extend(r.notes)
        errors.extend(r.errors)
    return mirrors, notes, errors


def needs_resolving(sources: list[SourceConfig]) -> bool:
    """True if any source leaves a dimension open (so listing is required)."""
    from mmw.providers import get_provider
    return any(get_provider(src.provider).needs_resolving(src) for src in sources)


def _covered(cfg, unit) -> bool:
    """Does any configured source still ask for `unit`?"""
    from mmw.providers import get_provider
    return any(get_provider(s.provider).covers(s, unit) for s in cfg.sources)


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
            held = pool.mirrors(configured)
    except Exception:
        return []
    # A suite, component or arch the config no longer asks for is gone, even
    # though its repository is still configured (see `SourceConfig.covers`).
    return [m for m in held if _covered(cfg, m)]


def uncovered_mirror_names(cfg, pool) -> list[str]:
    """Mirrors the pool holds for a still-configured repository that the
    config no longer covers — what `Pool.prune_orphans(dropped_mirrors=…)`
    drops. Mirrors of an unconfigured repository are not listed: the prune
    already drops those by URI."""
    configured = sorted({s.identity for s in cfg.sources})
    return [m.name for m in pool.mirrors(configured)
            if not _covered(cfg, m)]


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


def network_mirrors(network, cfg, mirror_dir, *, include_unpublishable: bool = False) -> list[MirrorConfig]:
    """The mirrors a network publishes.

    `network.select` filters what the pool actually holds, by dimension. An
    empty `select` means every mirrored repository; an empty dimension inside a
    select means all of that dimension.

    Selecting by dimension rather than by a frozen list of mirror names is what
    makes "this network is amd64" survive a new architecture appearing upstream
    — the old name list silently stopped publishing anything it did not already
    name, and said nothing about it.

    Units whose provider cannot publish yet (`publishes = False`) are left out
    of an implicit "everything" network — it keeps publishing what it can —
    but kept when a `select` names them, so publish can refuse that loudly.
    `include_unpublishable` returns them regardless.
    """
    from mmw.providers import get_provider
    available = available_mirrors(cfg, mirror_dir)
    if network.select:
        return [m for m in available if any(sel.matches(m) for sel in network.select)]
    if include_unpublishable:
        return available
    return [m for m in available if get_provider(m.provider).publishes]


def network_mirror_names(network, cfg, mirror_dir) -> list[str]:
    return [m.name for m in network_mirrors(network, cfg, mirror_dir)]
