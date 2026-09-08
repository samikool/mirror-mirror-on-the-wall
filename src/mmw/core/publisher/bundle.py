"""Bundle a network into one .tar.zst for transfer across an airgap.

Same pipeline as publish (`publisher/__init__.py:publish_network`): one
refs⋈packages enumeration per mirror_base, ordered pkg → meta → release, the
same dist-upgrade and key steps in the same order. Only the sink differs —
`BundleSink` instead of `TreeSink` — which is exactly the point: if a bundle
and a publish ever disagree about what a network contains, that is a sink bug,
not two independently-maintained pipelines drifting apart.

Deltas come from the on-disk manifest ledger (`manifest.py`), so a re-bundle a
month later carries what changed since the last one and nothing else.
"""
from pathlib import Path

from mmw.core.config import Config, NetworkConfig
from mmw.core.pool import Pool

from mmw.core.publisher.dist_upgrade import (
    dist_upgrade_info,
    publish_changelogs,
    write_upgrade_md,
)
from mmw.core.publisher.links import publish_keys, publish_group
from mmw.core.publisher.manifest import (
    MANIFEST_SUFFIX,
    ManifestHeader,
    bump_stamp,
    latest_manifest_path,
    ledger_dir,
    now_stamp,
    read_manifest,
    write_manifest,
)
from mmw.core.publisher.nginx import ROOT_PLACEHOLDER, nginx_files
from mmw.core.publisher.results import BundleResult
from mmw.core.publisher.sinks import BundleSink
from mmw.core.resolve import network_mirrors
from mmw.resources import apply_script_bytes


def bundle_output_path(network: NetworkConfig, created: str,
                       out: Path | None = None) -> Path:
    """Where this bundle lands. `out` may be a file or a directory."""
    name = f"{network.name}-{created}.tar.zst"
    if out is not None:
        return out / name if out.is_dir() or not out.suffix else out
    if network.bundle_dir is None:
        raise ValueError(
            f"network '{network.name}' has no bundle destination — "
            f"set bundle_dir in mmw.toml or pass -o"
        )
    return network.bundle_dir / name


def bundle_network(
    network: NetworkConfig,
    cfg: Config,
    mirror_dir: Path,
    out_path: Path | None = None,
    full: bool = False,
    level: int = 3,
    progress=None,
    stop_event=None,
    mirror_start_callback=None,
) -> BundleResult:
    """Materialize all of a network's mirrors into one `.tar.zst`.

    Mirrors are grouped by mirror_base exactly as `publish_network` groups
    them, and pushed through the same `publish_group` enumeration — a
    `BundleSink` in place of a `TreeSink` is the only difference. The delta
    against the previous bundle (`full=False`, the default) comes from the
    newest manifest in `{mirror_dir}/bundles/{network.name}/`; `full=True`
    resends everything but still reads that same manifest to compute what
    should be deleted — `--full` means "send everything", not "forget what
    the far side has" (a full bundle that skipped this would strand every
    file the far side no longer wants, permanently: the new manifest no
    longer lists the path either, so no future delta could delete it).
    """
    created = now_stamp()
    manifest_dir = ledger_dir(mirror_dir, network.name)
    # now_stamp() has 1-second resolution: two bundles started in the same
    # wall-clock second would otherwise silently overwrite each other's
    # tarball and manifest. Bump forward until the stamp collides with
    # neither — bundle_output_path() also validates the destination
    # (raises ValueError with no bundle_dir/out), so that check still
    # happens on the very first iteration, unchanged.
    while True:
        target = bundle_output_path(network, created, out_path)
        if not target.exists() and not (manifest_dir / f"{created}{MANIFEST_SUFFIX}").exists():
            break
        created = bump_stamp(created)

    # The delete set always comes from the latest manifest, `full` or not —
    # see the docstring above. `BundleSink`'s own `full` flag is what makes a
    # full bundle resend everything despite `prev` being populated here.
    prev_path = latest_manifest_path(mirror_dir, network.name)
    prev: dict[str, str] = {}
    previous = None
    if prev_path is not None:
        prev_header, prev = read_manifest(prev_path)
        previous = prev_header.created

    header = ManifestHeader(network=network.name, created=created,
                            previous=previous, full=full)
    sink = BundleSink(target, header, prev, apply_script_bytes(),
                      level=level, prune=network.prune, full=full)
    result = BundleResult(network_name=network.name, out_path=target,
                          created=created, previous=previous, full=full)

    mirror_map = {m.name: m for m in network_mirrors(network, cfg, mirror_dir)}
    selected_names = list(mirror_map)

    # Group by mirror_base, preserving selection order — same as publish.
    groups: dict[str, list] = {}
    for name in selected_names:
        mirror = mirror_map.get(name)
        if mirror is None:
            continue
        groups.setdefault(mirror.mirror_base, []).append(mirror)

    # A mirror whose publish base nests inside another's must never be pruned
    # by the outer group's finalize — same reasoning as publish_network.
    all_bases = set(groups)

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

        published_mirrors = [mirror_map[n] for n in selected_names if n in mirror_map]

        if network.dist_upgrade and not (stop_event and stop_event.is_set()):
            paths, warnings = dist_upgrade_info(network, cfg, pool)
            result.dist_upgrade_warnings = warnings
            publish_changelogs(sink, pool, result)
            write_upgrade_md(sink, network, pool, paths, result.dist_upgrade_warnings,
                             mirrors=published_mirrors)

        # nginx and keys are placed inside this `with Pool(...)` block, at the
        # same nesting depth as `publish_network`'s own copy of this step
        # (`publisher/__init__.py`), purely so the two functions stay visibly
        # parallel — neither step actually touches `pool`. Both must still
        # run before `sink.close()` below: BundleSink writes the whole
        # archive there, so anything handed to it after close() is silently
        # dropped — publish_network had exactly this bug for keys once, see
        # links.py.
        if not (stop_event and stop_event.is_set()):
            if network.nginx:
                for rel, data in nginx_files(published_mirrors, ROOT_PLACEHOLDER).items():
                    sink.write(rel, data)

            key_errors = publish_keys(sink, mirror_dir, published_mirrors)
            result.errors.extend(key_errors)

    if stop_event and stop_event.is_set():
        result.errors.append("stopped before the bundle was written")
        return result

    # No try/finally around close(), unlike publish_network's `finally:
    # sink.close()`. Deliberate, not an oversight — and the two sinks are not
    # symmetric here. TreeSink writes live as link()/write() are called, so
    # its close() (a no-op prune-total flush) is harmless to run even after a
    # mid-loop exception: whatever got linked before the failure is already
    # on disk either way. BundleSink is the opposite: link()/write() only
    # append `(rel, checksum)` rows and pool-blob references to in-memory
    # lists — the tar is not written at all until close() runs. Enumeration
    # raising partway through a network's groups (a DB error, a stopped
    # thread) means `sink._send`/`manifest_rows` hold an incomplete state; a
    # `finally: sink.close()` here would still happily write that partial
    # state out as a complete tarball plus a manifest ledger entry claiming
    # to be the network's whole snapshot — exactly the "the far side is
    # missing files the ledger says it has" failure mode `reconcile_refs`'s
    # one-transaction design exists to prevent on the sync side. Letting the
    # exception propagate past this function without calling close() at all
    # is what keeps a failed bundle from ever landing on disk or in the
    # ledger.
    before_close = len(sink.errors)
    sink.close()
    # close() can append its own errors (a blob resized or vanished between
    # link() and the tar write) that never passed through link(), so they
    # were never sliced into a MirrorPublishResult or into key_errors above.
    # Slice from here so those — and only those — land in result.errors
    # once, instead of either duplicating what a mirror result already
    # carries or dropping close()-time failures silently.
    result.errors.extend(sink.errors[before_close:])

    # The ledger manifest is written only after the tarball lands: a crash
    # between the two must not leave a manifest claiming a bundle that does
    # not exist, or the next delta would skip files the far side never
    # received. close() itself is tmp-then-rename, so this ordering makes the
    # pair atomic in the right direction.
    manifest_path = manifest_dir / f"{created}{MANIFEST_SUFFIX}"
    write_manifest(manifest_path, header, sink.manifest_rows)
    (target.parent / "apply.py").write_bytes(apply_script_bytes())

    result.manifest_path = manifest_path
    result.sent = sink.sent
    result.sent_bytes = sink.sent_bytes
    result.deleted = len(sink.deleted)
    return result
