"""Publish pipeline: materialize per-network trees from the pool DB.

Public API:
    publish_network / publish_group   — do the work
    dry_run_network / dry_run_mirror  — read-only estimates
    publish_base                      — destination path derivation
    NetworkPublishResult / MirrorPublishResult
"""
from pathlib import Path

from mmw.core.config import Config, NetworkConfig
from mmw.core.pool import Pool

from mmw.core.publisher.dist_upgrade import (
    dist_upgrade_info,
    parse_meta_release_lts,
    publish_changelogs,
    write_upgrade_md,
)
from mmw.core.publisher.links import (
    ROOT_RELATIVE_PREFIX,
    hardlink_or_copy,
    is_package_file,
    publish_group,
    publish_keys,
)
from mmw.core.resolve import network_mirrors
from mmw.core.publisher.results import MirrorPublishResult, NetworkPublishResult
from mmw.core.publisher.sinks import TreeSink
from mmw.core.publisher.stats import (
    dry_run_mirror,
    dry_run_network,
    dst_stats_apt,
    dst_stats_apt_flat,
    publish_base,
)

__all__ = [
    "MirrorPublishResult",
    "NetworkPublishResult",
    "ROOT_RELATIVE_PREFIX",
    "TreeSink",
    "dist_upgrade_info",
    "dry_run_mirror",
    "dry_run_network",
    "dst_stats_apt",
    "dst_stats_apt_flat",
    "hardlink_or_copy",
    "is_package_file",
    "parse_meta_release_lts",
    "publish_base",
    "publish_changelogs",
    "publish_group",
    "publish_keys",
    "publish_network",
    "write_upgrade_md",
]


def publish_network(
    network: NetworkConfig,
    cfg: Config,
    mirror_dir: Path,
    progress=None,
    stop_event=None,
    mirror_start_callback=None,
) -> NetworkPublishResult:
    """Materialize all of a network's mirrors from the pool DB.

    Mirrors are processed in groups sharing a mirror_base (apt component
    mirrors of one source URL publish as a unit); each group's destination is
    built packages-first, release-files-last, then finalized against the refs
    set through one `TreeSink` shared across every group.
    """
    result = NetworkPublishResult(network_name=network.name, dry_run=False)

    if network.root is None:
        r = MirrorPublishResult(mirror_name=network.name)
        r.errors.append(
            f"network '{network.name}' has no publish root — use `mmw bundle` instead"
        )
        result.mirrors.append(r)
        return result

    mirror_map = {m.name: m for m in network_mirrors(network, cfg, mirror_dir)}
    selected_names = list(mirror_map)

    # Group by mirror_base, preserving selection order.
    groups: dict[str, list] = {}
    for name in selected_names:
        mirror = mirror_map.get(name)
        if mirror is None:
            r = MirrorPublishResult(mirror_name=name)
            r.errors.append(f"mirror '{name}' not found in config")
            result.mirrors.append(r)
            continue
        groups.setdefault(mirror.mirror_base, []).append(mirror)

    # A mirror whose publish base nests inside another's (custom repos at
    # host/repo and host/repo/sub) must never be pruned by the outer group.
    all_bases = set(groups)

    sink = TreeSink(network.root, progress=progress, stop_event=stop_event)
    try:
        with Pool(mirror_dir, read_only=True) as pool:
            total_groups = len(groups)
            for idx, (base, mirrors) in enumerate(groups.items()):
                if stop_event and stop_event.is_set():
                    break
                if mirror_start_callback:
                    mirror_start_callback(mirrors[0].name, idx + 1, total_groups)
                nested = {b for b in all_bases if b != base and b.startswith(base + "/")}
                result.mirrors.append(publish_group(
                    mirrors, sink, pool,
                    progress=progress, stop_event=stop_event,
                    prune=network.prune, prune_exclude=nested,
                ))

            if network.dist_upgrade and not (stop_event and stop_event.is_set()):
                paths, warnings = dist_upgrade_info(network, cfg, pool)
                result.dist_upgrade_paths = paths
                result.dist_upgrade_warnings = warnings
                publish_changelogs(sink, pool, result)
                write_upgrade_md(sink, network, pool, paths, result.dist_upgrade_warnings,
                                 mirrors=[mirror_map[n] for n in selected_names
                                          if n in mirror_map])

            # Must run before sink.close(): BundleSink (Task 6) finalizes the
            # whole archive there, so anything written after close is dropped.
            if not (stop_event and stop_event.is_set()):
                published_mirrors = [mirror_map[n] for n in selected_names if n in mirror_map]

                if network.nginx:
                    from mmw.core.publisher.nginx import nginx_files
                    for rel, data in nginx_files(published_mirrors, str(network.root)).items():
                        sink.write(rel, data)

                key_errors = publish_keys(sink, mirror_dir, published_mirrors)
                if key_errors:
                    r = MirrorPublishResult(mirror_name="keys")
                    r.errors.extend(key_errors)
                    result.mirrors.append(r)
    finally:
        sink.close()

    return result
