"""Destination-size accounting and dry-run reports.

Never walks mirror source trees — sizes come from the pool DB and from the
metadata indexes already present at the publish destination.
"""
from pathlib import Path

from mmw.core import disk_io
from mmw.core.config import Config, MirrorConfig, NetworkConfig
from mmw.core.pool import Pool

from mmw.core.publisher.results import MirrorPublishResult, NetworkPublishResult, NetworkTotals


def publish_base(network: NetworkConfig, mirror: MirrorConfig) -> Path:
    """Derive the publish directory for one mirror within a network.

    source_url = "https://archive.ubuntu.com/ubuntu/"
    → network.root / "archive.ubuntu.com" / "ubuntu"
    """
    if network.root is None:
        raise ValueError(
            f"network '{network.name}' has no publish root — it can only be bundled"
        )
    return network.root / mirror.mirror_base


def dst_index_path(dst_root: Path, mirror: MirrorConfig) -> Path | None:
    """The published index `dst_packages` reads for this mirror, if any —
    its mtime says whether figures read from it are still current."""
    from mmw.providers import get_provider
    return get_provider(mirror.provider).published_index(dst_root, mirror)


def dst_packages(dst_root: Path, mirror: MirrorConfig) -> list:
    """The packages a published index at the destination lists (empty when
    nothing is published yet, or for a provider with no index we read)."""
    from mmw.providers import get_provider
    p = dst_index_path(dst_root, mirror)
    if p is None:
        return []
    return get_provider(mirror.provider).published_packages(p)


def unique_packages(pkgs, seen: dict[str, int]) -> None:
    """Add `pkgs` to `seen` (checksum → size): a file several published
    indexes list — or several mirrors share — is one entry."""
    for pkg in pkgs:
        seen[pkg.checksum or pkg.repo_path] = pkg.size


def network_totals(mirror_names: list[str], pool: Pool,
                   published: dict[str, int]) -> NetworkTotals:
    """Network-wide dry-run numbers, each file once: the pool for what the
    network holds, `published` (filled by `dry_run_mirror`) for what the
    destination already has."""
    files, size = pool.unique_stats(mirror_names)
    return NetworkTotals(files=files, bytes=size, published_files=len(published),
                         published_bytes=sum(published.values()))


def dry_run_mirror(
    mirror: MirrorConfig,
    network: NetworkConfig,
    mirror_dir: Path,
    pool: Pool,
    published: dict[str, int] | None = None,
) -> MirrorPublishResult:
    """Estimate publish work from the pool DB and the destination's own index.

    These are estimates: pool refs and destination index counts measure
    slightly different sets (e.g. source packages with include_sources).
    `published`, when given, collects what the destination already has
    (checksum → size) for `network_totals`.
    """
    result = MirrorPublishResult(mirror_name=mirror.name)

    # Mirror totals come from the pool DB — one query, no disk scan
    total_files, total_bytes = pool.mirror_stats(mirror.name)
    if total_files == 0 and not pool.ref_map(mirror.name):
        result.errors.append("not synced yet — no pool refs for this mirror")
        return result

    # Already-published count comes from the destination's own metadata index
    # (the same files the mirror put there); falls back to 0 on first publish.
    dst_root = publish_base(network, mirror)
    already_files = 0
    already_bytes = 0
    if disk_io.ops.exists(dst_root):
        pkgs = dst_packages(dst_root, mirror)
        already_files, already_bytes = len(pkgs), sum(p.size for p in pkgs)
        if published is not None:
            unique_packages(pkgs, published)

    result.skipped = already_files
    result.skipped_bytes = already_bytes
    result.linked = max(0, total_files - already_files)
    result.linked_bytes = max(0, total_bytes - already_bytes)
    return result


def dry_run_network(
    network: NetworkConfig,
    cfg: Config,
    mirror_dir: Path,
) -> NetworkPublishResult:
    """Read-only report — pool DB for mirror sizes, filesystem scan only for destination."""
    result = NetworkPublishResult(network_name=network.name, dry_run=True)

    # A bundle-only network has no publish root — `publish_base()` below
    # would raise ValueError for every mirror. Mirror `publish_network`'s own
    # guard (publisher/__init__.py) and its exact message, rather than
    # letting that exception surface here: this function is read-only and
    # must never crash a caller iterating several networks in one dry-run.
    if network.root is None:
        r = MirrorPublishResult(mirror_name=network.name)
        r.errors.append(
            f"network '{network.name}' has no publish root — use `mmw bundle` instead"
        )
        result.mirrors.append(r)
        return result

    from mmw.core.resolve import network_mirrors
    mirror_map = {m.name: m for m in network_mirrors(network, cfg, mirror_dir)}
    selected_names = list(mirror_map)

    published: dict[str, int] = {}
    with Pool(mirror_dir, read_only=True) as pool:
        for name in selected_names:
            mirror = mirror_map.get(name)
            if mirror is None:
                r = MirrorPublishResult(mirror_name=name)
                r.errors.append(f"mirror '{name}' not found in config")
                result.mirrors.append(r)
                continue
            result.mirrors.append(dry_run_mirror(mirror, network, mirror_dir, pool, published))
        result.totals = network_totals(selected_names, pool, published)
        from mmw.providers import preview_all
        preview_all(network, cfg, pool, mirror_dir, result)

    return result
