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
import hashlib
from pathlib import Path

from mmw.core.config import Config, NetworkConfig
from mmw.core.pool import Pool

from mmw.core.publisher.dist_upgrade import (
    dist_upgrade_info,
    publish_changelogs,
    write_upgrade_md,
)
from mmw.core.publisher.links import (
    publish_group,
    publish_keys,
    publish_upstream_files,
    reconcile_extra_prefix,
    upstream_files,
    upstream_file_hosts,
    upstream_file_live_dirs,
)
from mmw.core.publisher.manifest import (
    MANIFEST_SUFFIX,
    ManifestHeader,
    bump_stamp,
    latest_manifest_path,
    ledger_dir,
    now_stamp,
    read_manifest,
    union_manifests,
    write_manifest,
)
from mmw.core.certs import CHANGELOGS_HOST, cert_dir, servable_paths
from mmw.core.publisher.nginx import ROOT_PLACEHOLDER, nginx_files
from mmw.core.publisher.results import BundleResult
from mmw.core.publisher.sinks import TLS_DIR, BundleSink
from mmw.core.resolve import network_mirrors
from mmw.resources import apply_script_bytes


def is_explicit_file_out(out: Path | None) -> bool:
    """True when `out` names one specific file rather than a directory.

    The distinction is load-bearing in two places and neither is obvious:

    - The output path is only stamp-derived for a directory target. For an
      explicit file `bundle_output_path` returns `out` verbatim, so bumping
      the stamp cannot change it — `bundle_network`'s collision loop, written
      for the directory case, spun at 100% CPU forever the moment that file
      already existed.
    - One explicit file cannot hold several networks. `mmw bundle --out
      x.tar.zst` with no `-n` had each network silently overwrite the last,
      leaving one tarball named for a network it mostly doesn't contain.
      Callers iterating networks must refuse that up front; this is the
      predicate to refuse on.
    """
    return out is not None and bool(out.suffix) and not out.is_dir()


def bundle_output_path(network: NetworkConfig, created: str,
                       out: Path | None = None, compress: bool = True) -> Path:
    """Where this bundle lands. `out` may be a file or a directory."""
    name = f"{network.name}-{created}.{'tar.zst' if compress else 'tar'}"
    if out is not None:
        return out if is_explicit_file_out(out) else out / name
    if network.bundle_dir is None:
        raise ValueError(
            f"network '{network.name}' has no bundle destination — "
            f"set bundle_dir in mmw.toml or pass --out"
        )
    return network.bundle_dir / name


_CHECKSUM_CHUNK = 1 << 20


def _write_checksum_file(target: Path) -> Path:
    """Write `{target}.sha256` beside the bundle, in `sha256sum -c` format.

    The far side can then tell a bad *transfer* from a bad *bundle* in seconds,
    with no mmw installed — `sha256sum -c lab-….tar.zst.sha256` is enough. That
    distinction matters because a tarball truncated on a member boundary still
    extracts cleanly, so without this the first sign of a bad copy is
    `apply.py --verify` an hour later, or nothing at all.

    Written tmp-then-rename for the same reason the tarball and the manifest
    are: a half-written checksum file that happens to parse is worse than none.
    """
    h = hashlib.sha256()
    with target.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHECKSUM_CHUNK), b""):
            h.update(chunk)
    dest = target.with_name(target.name + ".sha256")
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_text(f"{h.hexdigest()}  {target.name}\n")
    tmp.replace(dest)
    return dest


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
    compress: bool = True,
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
    explicit_file = is_explicit_file_out(out_path)
    if explicit_file:
        # An explicit `--out file` is stamp-INVARIANT: bundle_output_path
        # returns it verbatim, so the collision loop below could never
        # produce a different path and spun forever at 100% CPU the moment
        # that file existed. There is also nothing safe to invent here — the
        # operator named one file, so overwriting it silently or writing
        # beside it under a name they didn't ask for are both worse than
        # saying so.
        target = bundle_output_path(network, created, out_path, compress=compress)
        if target.exists():
            raise ValueError(
                f"{target} already exists — refusing to overwrite an explicit "
                f"--out target. Remove it, or pass a directory to --out and let "
                f"mmw name the bundle."
            )
        while (manifest_dir / f"{created}{MANIFEST_SUFFIX}").exists():
            created = bump_stamp(created)
    else:
        # now_stamp() has 1-second resolution: two bundles started in the same
        # wall-clock second would otherwise silently overwrite each other's
        # tarball and manifest. Bump forward until the stamp collides with
        # neither — bundle_output_path() also validates the destination
        # (raises ValueError with no bundle_dir/out), so that check still
        # happens on the very first iteration, unchanged.
        while True:
            target = bundle_output_path(network, created, out_path, compress=compress)
            if (not target.exists()
                    and not (manifest_dir / f"{created}{MANIFEST_SUFFIX}").exists()):
                break
            created = bump_stamp(created)

    # The delete set always comes from the ledger, `full` or not — see the
    # docstring above. `BundleSink`'s own `full` flag is what makes a full
    # bundle resend everything despite `prev` being populated here.
    prev_path = latest_manifest_path(mirror_dir, network.name)
    prev: dict[str, str] = {}
    previous = None
    if prev_path is not None:
        prev_header, prev = read_manifest(prev_path)
        previous = prev_header.created
    if full:
        # A delta's delete set is correctly `latest − keep`: the far side is
        # known to be at `latest`. `--full` is reached precisely when it is
        # NOT — and `latest − keep` then misses everything dropped more than
        # one generation ago, since no manifest since has mentioned it. Union
        # the retained ledger so a far side several bundles behind is
        # actually repaired. `previous` still names the latest manifest; only
        # the delete set widens.
        prev = {**union_manifests(mirror_dir, network.name), **prev}

    # Checked before any work: an opted-in TLS bundle that cannot carry its
    # certificate must not quietly ship as HTTP-only after an hour of
    # archiving — the far side would only find out when clients refuse it.
    tls_files: dict[str, bytes] = {}
    if network.bundle_tls:
        tls_paths = servable_paths(network, cfg)
        if tls_paths is None:
            raise ValueError(
                f"network '{network.name}' has bundle_tls = true but no usable "
                f"signed certificate in {cert_dir(network, cfg)} — "
                f"run `mmw certs -n {network.name} --ca CA.crt --ca-key CA.key`"
            )
        tls_files = {"mirror.crt": tls_paths[0].read_bytes(),
                     "mirror.key": tls_paths[1].read_bytes()}

    header = ManifestHeader(network=network.name, created=created,
                            previous=previous, full=full)
    sink = BundleSink(target, header, prev, apply_script_bytes(),
                      level=level, prune=network.prune, full=full,
                      progress=progress, compress=compress, tls_files=tls_files)
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
    # Same as publish_network: known before any group runs, so a key inside a
    # repository's directory is in that group's keep and never lands on the
    # far side's delete list.
    upstream_paths = upstream_files(
        mirror_dir, [mirror_map[n] for n in selected_names if n in mirror_map])

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
                keep_extra=set(upstream_paths),
            ))

        published_mirrors = [mirror_map[n] for n in selected_names if n in mirror_map]

        if network.dist_upgrade and not (stop_event and stop_event.is_set()):
            paths, warnings = dist_upgrade_info(network, cfg, pool, mirror_dir)
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
            nginx_rels: set[str] = set()
            if network.nginx:
                # Only bundle_tls puts a certificate on the far side; a
                # `certs` network without it is HTTP there, whatever it is here.
                tls = ((f"{ROOT_PLACEHOLDER}/{TLS_DIR}/mirror.crt",
                        f"{ROOT_PLACEHOLDER}/{TLS_DIR}/mirror.key")
                       if tls_files else None)
                extra = upstream_file_hosts(upstream_paths)
                if network.dist_upgrade:
                    extra.append(CHANGELOGS_HOST)
                for rel, data in nginx_files(published_mirrors, ROOT_PLACEHOLDER,
                                             tls=tls, extra_hosts=extra).items():
                    sink.write(rel, data)
                    nginx_rels.add(rel)

            key_errors, key_rels = publish_keys(sink, mirror_dir, published_mirrors)
            key_errors += publish_upstream_files(sink, upstream_paths)
            result.own_errors.extend(key_errors)

            # `nginx/` and `keys/` live outside every mirror_base, so no
            # `finalize_group` above ever looked at them and nothing was ever
            # removed from either. Dropping ports.ubuntu.com from a network
            # left `nginx/sites/ports.ubuntu.com.conf` behind, and
            # `mirror.conf`'s `include nginx/sites/*.conf` kept loading it —
            # a DNS-spoofed client then silently got a stale mirror instead
            # of the clear failure it should have got. Reconciled here as
            # their own groups, on exactly the same contract.
            if network.prune:
                reconcile_extra_prefix(sink, "nginx", nginx_rels)
                reconcile_extra_prefix(sink, "keys", key_rels)
                sink.prune_stale_bases(
                    all_bases | upstream_file_live_dirs(upstream_paths, all_bases))

    if stop_event and stop_event.is_set():
        result.own_errors.append("stopped before the bundle was written")
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
    result.own_errors.extend(sink.errors[before_close:])

    # The ledger manifest is written only after the tarball lands: a crash
    # between the two must not leave a manifest claiming a bundle that does
    # not exist, or the next delta would skip files the far side never
    # received. close() itself is tmp-then-rename, so this ordering makes the
    # pair atomic in the right direction.
    #
    # `sink.written_header`, not the `header` built above: only the former
    # carries the `file_count` that actually shipped, so writing the latter
    # made the ledger a not-quite-faithful record of the bundle it describes.
    manifest_path = manifest_dir / f"{created}{MANIFEST_SUFFIX}"
    write_manifest(manifest_path, sink.written_header or header, sink.manifest_rows)
    (target.parent / "apply.py").write_bytes(apply_script_bytes())

    # A producing-side integrity artifact, so the far side can tell a bad
    # transfer from a bad bundle in seconds instead of discovering it an hour
    # into `apply.py --verify` (or, worse, not at all — a tarball that
    # truncates on a member boundary still applies). sha256sum-compatible
    # format, so `sha256sum -c lab-….tar.zst.sha256` just works with no mmw
    # on the far side at all.
    result.checksum_path = _write_checksum_file(target)

    result.manifest_path = manifest_path
    result.sent = sink.sent
    result.sent_bytes = sink.sent_bytes
    result.deleted = len(sink.deleted)
    return result
