"""Destination-size accounting and dry-run reports.

Never walks mirror source trees — sizes come from the pool DB and from the
metadata indexes already present at the publish destination.
"""
from pathlib import Path
from urllib.parse import urlparse

from mmw.core.compress import decompress
from mmw.core.config import Config, MirrorConfig, NetworkConfig
from mmw.core.pool import Pool
from mmw.providers.apt import _parse_packages, _parse_packages_flat

from mmw.core.publisher.results import MirrorPublishResult, NetworkPublishResult


def publish_base(network: NetworkConfig, mirror: MirrorConfig) -> Path:
    """Derive the publish directory for one mirror within a network.

    source_url = "https://archive.ubuntu.com/ubuntu/"
    → network.root / "archive.ubuntu.com" / "ubuntu"
    """
    parsed = urlparse(mirror.source_url)
    host = parsed.hostname or "unknown"
    if parsed.port:
        host = f"{host}_{parsed.port}"
    upstream_path = parsed.path.strip("/")
    return network.root / host / upstream_path


def dst_stats_apt(dst_root: Path, suite: str, component: str, arch: str) -> tuple[int, int]:
    """Count (files, bytes) from a published Packages.xz/gz at the destination."""
    packages_base = dst_root / "dists" / suite / component / f"binary-{arch}" / "Packages"
    for suffix in (".xz", ".gz", ""):
        p = Path(str(packages_base) + suffix)
        if p.exists():
            try:
                text = decompress(p.read_bytes(), p.name).decode("utf-8", errors="replace")
                pkgs = _parse_packages(text, "")
                return len(pkgs), sum(pkg.size for pkg in pkgs)
            except Exception:
                break
    return 0, 0


def dst_stats_apt_flat(dst_root: Path) -> tuple[int, int]:
    """Count (files, bytes) from a published Packages.xz/gz at a flat repo destination."""
    packages_base = dst_root / "Packages"
    for suffix in (".xz", ".gz", ""):
        p = Path(str(packages_base) + suffix)
        if p.exists():
            try:
                text = decompress(p.read_bytes(), p.name).decode("utf-8", errors="replace")
                pkgs = _parse_packages_flat(text, "")
                return len(pkgs), sum(pkg.size for pkg in pkgs)
            except Exception:
                break
    return 0, 0


def dry_run_mirror(
    mirror: MirrorConfig,
    network: NetworkConfig,
    mirror_dir: Path,
    pool: Pool,
) -> MirrorPublishResult:
    """Estimate publish work from the pool DB and the destination's own index.

    These are estimates: pool refs and destination index counts measure
    slightly different sets (e.g. source packages with include_sources).
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
    if dst_root.exists():
        if mirror.provider == "apt":
            already_files, already_bytes = dst_stats_apt(
                dst_root,
                mirror.suite or "",
                mirror.component or "main",
                mirror.arch,
            )
        elif mirror.provider == "apt-flat":
            already_files, already_bytes = dst_stats_apt_flat(dst_root)

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
    from mmw.core.publisher.dist_upgrade import dist_upgrade_info

    result = NetworkPublishResult(network_name=network.name, dry_run=True)
    from mmw.core.resolve import network_mirrors
    mirror_map = {m.name: m for m in network_mirrors(network, cfg, mirror_dir)}
    selected_names = list(mirror_map)

    with Pool(mirror_dir, read_only=True) as pool:
        for name in selected_names:
            mirror = mirror_map.get(name)
            if mirror is None:
                r = MirrorPublishResult(mirror_name=name)
                r.errors.append(f"mirror '{name}' not found in config")
                result.mirrors.append(r)
                continue
            result.mirrors.append(dry_run_mirror(mirror, network, mirror_dir, pool))

    if network.dist_upgrade:
        with Pool(mirror_dir, read_only=True) as pool:
            result.dist_upgrade_paths, result.dist_upgrade_warnings = dist_upgrade_info(network, cfg, pool)

    return result
