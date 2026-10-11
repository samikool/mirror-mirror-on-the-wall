"""Publish pipeline: materialize per-network trees from the pool DB.

Public API:
    publish_network / publish_group   — do the work
    publish_snapshot                  — lay a snapshot out again, exactly
    dry_run_network / dry_run_mirror  — read-only estimates
    publish_base                      — destination path derivation
    NetworkPublishResult / MirrorPublishResult
"""
from pathlib import Path

from mmw.core.config import Config, NetworkConfig
from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool

from mmw.core.publisher.bundle import bundle_network, bundle_output_path
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
    publish_upstream_files,
    reconcile_extra_prefix,
    upstream_files,
    upstream_file_hosts,
    upstream_file_live_dirs,
)
from mmw.core.resolve import network_mirrors
from mmw.core.publisher.results import BundleResult, MirrorPublishResult, NetworkPublishResult
from mmw.core.publisher.sinks import TreeSink
from mmw.core.publisher.stats import (
    dry_run_mirror,
    dry_run_network,
    dst_packages,
    network_totals,
    publish_base,
)

__all__ = [
    "BundleResult",
    "MirrorPublishResult",
    "NetworkPublishResult",
    "ROOT_RELATIVE_PREFIX",
    "TreeSink",
    "bundle_network",
    "bundle_output_path",
    "dist_upgrade_info",
    "dry_run_mirror",
    "dry_run_network",
    "dst_packages",
    "hardlink_or_copy",
    "network_totals",
    "is_package_file",
    "parse_meta_release_lts",
    "publish_base",
    "publish_changelogs",
    "publish_group",
    "publish_keys",
    "publish_network",
    "publish_snapshot",
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

    from mmw.core.certs import servable_paths
    # Served straight from cert_dir: a publish destination is on this
    # machine, and the key never enters a served tree.
    tls_paths = servable_paths(network, cfg) if network.nginx else None
    tls = (str(tls_paths[0]), str(tls_paths[1])) if tls_paths else None
    sink = TreeSink(network.root, progress=progress, stop_event=stop_event)
    try:
        with Pool(mirror_dir, read_only=True) as pool:
            walk_network(network, cfg, mirror_dir, sink, pool, result,
                         nginx_root=str(network.root), tls=tls, prune=network.prune,
                         progress=progress, stop_event=stop_event,
                         mirror_start_callback=mirror_start_callback)
    finally:
        sink.close()
    if not (stop_event and stop_event.is_set()):
        _record_published(mirror_dir, network.root, network.name, "live")

    return result


def _record_published(mirror_dir: Path, root: Path, network: str, target: str) -> None:
    with Pool(mirror_dir) as pool:
        pool.record_published(str(Path(root).resolve()), network, target)


# Trust roots: placed after everything they reference.
_RELEASE_NAMES = frozenset({"InRelease", "Release", "Release.gpg",
                            "repomd.xml", "repomd.xml.asc", "repomd.xml.key"})
_INDEX_DIRS = frozenset({"dists", "repodata", "by-hash"})
_INDEX_PREFIXES = ("Packages", "Sources", "Translation", "Contents")


def _snapshot_kind(rel: str) -> str:
    """pkg / meta / release for a snapshot row, which keeps no kind: read from
    the path. Only the publish ORDER depends on it (packages, then indexes,
    then release files), so a live root never has a Release pointing at a
    file not yet there."""
    parts = rel.split("/")
    if parts[-1] in _RELEASE_NAMES:
        return KIND_RELEASE
    if _INDEX_DIRS.intersection(parts[:-1]) or parts[-1].startswith(_INDEX_PREFIXES):
        return KIND_META
    return KIND_PKG


def publish_snapshot(
    network: NetworkConfig,
    cfg: Config,
    mirror_dir: Path,
    ref: str,
    *,
    root: Path | None = None,
    progress=None,
    stop_event=None,
    mirror_start_callback=None,
) -> NetworkPublishResult:
    """Lay snapshot `ref` (stamp, label or id) out at `root` (default: the
    network's root) exactly as it was — old packages come back, newer ones
    go (with `prune`). **Blocking.**

    The rows are the complete tree minus nginx, which is regenerated for
    `root` from the snapshot's recorded mirrors (same hostnames: a second
    directory needs its own address). Each mirror_base is finalized the way
    `publish_group` does it, so by-hash retention is the same.
    """
    from mmw.core.certs import CHANGELOGS_HOST as CHANGELOGS_DIR
    from mmw.core.snapshots import find_snapshot, recorded_bases

    result = NetworkPublishResult(network_name=network.name, dry_run=False)
    root = root if root is not None else network.root
    if root is None:
        raise ValueError(f"network '{network.name}' has no publish root — pass a directory")
    with Pool(mirror_dir, read_only=True) as pool:
        snap = find_snapshot(pool, network.name, ref)
        if not snap.complete:
            raise ValueError(f"snapshot {snap.stamp} is history only (imported from an old "
                             "bundle; its files may be gone) — it cannot be published")
        missing = pool.snapshot_missing(snap.id)
        if missing:
            raise ValueError(f"{missing} file(s) of snapshot {snap.stamp} are no longer in "
                             "the pool — it cannot be published")
        bases = recorded_bases(snap.settings)
        per_base = {b: MirrorPublishResult(mirror_name=b) for b in sorted(bases)}
        other = MirrorPublishResult(mirror_name="keys and extra files")
        keep: dict[str | None, set[str]] = {b: set() for b in bases}
        keep[None] = set()
        live: dict[str, set[str]] = {b: set() for b in bases}
        deferred: dict[str, list] = {KIND_META: [], KIND_RELEASE: []}

        sink = TreeSink(root, progress=progress, stop_event=stop_event)
        try:
            def place(row, base):
                rel, checksum, pool_path, size = row
                r = per_base[base] if base else other
                before = len(sink.errors)
                status = sink.link(Path(pool_path), rel, size, checksum, KIND_PKG)
                if status == "linked":
                    r.linked += 1
                    r.linked_bytes += size
                elif status == "skipped":
                    r.skipped += 1
                    r.skipped_bytes += size
                else:
                    r.errors.extend(sink.errors[before:] or [f"{rel}: link error"])
                if progress:
                    progress(rel, status)

            # Packages as the rows stream past; indexes and release files after.
            current = None
            for i, row in enumerate(pool.snapshot_blob_rows(snap.id)):
                if stop_event is not None and i % 1000 == 0 and stop_event.is_set():
                    break
                rel = row[0]
                base = next((b for b in bases if rel.startswith(b + "/")), None)
                keep[base].add(rel)
                if base is not None and base != current and mirror_start_callback:
                    current = base
                    mirror_start_callback(base, sorted(bases).index(base) + 1, len(bases))
                kind = _snapshot_kind(rel) if base else KIND_PKG
                if kind == KIND_PKG:
                    place(row, base)
                else:
                    live[base].add(row[1])
                    deferred[kind].append((row, base))
            for kind in (KIND_META, KIND_RELEASE):
                for i, (row, base) in enumerate(deferred[kind]):
                    if stop_event is not None and i % 1000 == 0 and stop_event.is_set():
                        break
                    place(row, base)

            if not (stop_event and stop_event.is_set()):
                nginx_rels: set[str] = set()
                if network.nginx:
                    from mmw.core.certs import servable_paths
                    from mmw.core.publisher.nginx import recorded_nginx_files
                    tls_paths = servable_paths(network, cfg)
                    tls = (str(tls_paths[0]), str(tls_paths[1])) if tls_paths else None
                    for rel, data in recorded_nginx_files(snap.settings, str(root),
                                                          tls=tls).items():
                        sink.write(rel, data)
                        nginx_rels.add(rel)
                if network.prune:
                    for b in bases:
                        nested = {x for x in bases if x != b and x.startswith(b + "/")}
                        r = per_base[b]
                        r.pruned, r.pruned_bytes = sink.finalize_group(
                            b, keep[b], live[b], nested)
                    extra = keep[None]
                    for prefix in ("keys", CHANGELOGS_DIR):
                        reconcile_extra_prefix(
                            sink, prefix, {p for p in extra if p.startswith(prefix + "/")})
                    reconcile_extra_prefix(sink, "nginx", nginx_rels)
                    if "UPGRADE.md" not in extra:
                        (Path(root) / "UPGRADE.md").unlink(missing_ok=True)
                    upstream = {p: None for p in extra
                                if not p.startswith(("keys/", CHANGELOGS_DIR + "/"))}
                    sink.prune_stale_bases(set(bases) | upstream_file_live_dirs(
                        upstream, set(bases)))
        finally:
            sink.close()

    result.mirrors.extend(per_base.values())
    if other.total_files or other.errors:
        result.mirrors.append(other)
    if not (stop_event and stop_event.is_set()):
        _record_published(mirror_dir, root, network.name, snap.stamp)
    return result


def walk_network(
    network: NetworkConfig,
    cfg: Config,
    mirror_dir: Path,
    sink,
    pool: Pool,
    result: NetworkPublishResult,
    *,
    nginx_root: str | None,
    tls: tuple[str, str] | None = None,
    prune: bool = False,
    progress=None,
    stop_event=None,
    mirror_start_callback=None,
) -> list:
    """Lay a network's complete tree into `sink` from `pool`: every group,
    dist-upgrade, nginx (only with `nginx_root` — its config embeds the
    destination), keys and mirrored upstream files. `publish_network` passes a
    `TreeSink`; taking a snapshot passes a recording sink. Returns the mirrors
    walked.
    """
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
    published_mirrors = [mirror_map[n] for n in selected_names if n in mirror_map]
    # Known before any group runs: a key inside a repository's directory must
    # be in that group's `keep`, not pruned and relinked every publish.
    upstream_paths = upstream_files(mirror_dir, published_mirrors)

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
            prune=prune, prune_exclude=nested,
            keep_extra=set(upstream_paths),
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
        nginx_rels: set[str] = set()
        if network.nginx and nginx_root is not None:
            from mmw.core.certs import CHANGELOGS_HOST
            from mmw.core.publisher.nginx import nginx_files
            extra = upstream_file_hosts(upstream_paths)
            if network.dist_upgrade:
                extra.append(CHANGELOGS_HOST)
            for rel, data in nginx_files(published_mirrors, nginx_root,
                                         tls=tls, extra_hosts=extra).items():
                sink.write(rel, data)
                nginx_rels.add(rel)

        # `publish_keys` returns (errors, published_rels). Assigning the
        # tuple to `key_errors` and extending a result's error list with
        # it produced `[[], set()]` — two bogus "errors" on every clean
        # publish, which is how this was caught.
        key_errors, key_rels = publish_keys(sink, mirror_dir, published_mirrors)
        key_errors += publish_upstream_files(sink, upstream_paths)
        if key_errors:
            r = MirrorPublishResult(mirror_name="keys")
            r.errors.extend(key_errors)
            result.mirrors.append(r)

        # The same three reconciliations `bundle_network` performs, for
        # the same reason: `nginx/` and `keys/` live outside every
        # mirror_base, and a `mirror_base` that no group produced any
        # more is never walked at all — so dropping a repository from a
        # network left its whole stale subtree, its nginx site file and
        # its keys being served indefinitely, with `prune = true` giving
        # no warning. Publish and bundle must agree here or the two
        # halves of the pair drift apart again.
        if prune:
            reconcile_extra_prefix(sink, "nginx", nginx_rels)
            reconcile_extra_prefix(sink, "keys", key_rels)
            sink.prune_stale_bases(
                all_bases | upstream_file_live_dirs(upstream_paths, all_bases))
    return published_mirrors
