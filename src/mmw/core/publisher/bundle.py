"""Bundle a network into one plain .tar for transfer across an airgap.

No compression: the payload is packages, already compressed, and zstd over
terabytes on one disk only made it slower. Compress the tar yourself if a
transfer medium wants it (apply.py then needs it decompressed first).

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

from mmw.core.publisher.manifest import (
    MANIFEST_SUFFIX,
    ManifestHeader,
    ledger_dir,
    write_manifest,
)
from mmw.core.certs import cert_dir, servable_paths
from mmw.core.publisher.nginx import ROOT_PLACEHOLDER, recorded_nginx_files
from mmw.core.publisher.results import BundleResult
from mmw.core.publisher.sinks import TLS_DIR, BundleSink
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
      x.tar` with no `-n` had each network silently overwrite the last,
      leaving one tarball named for a network it mostly doesn't contain.
      Callers iterating networks must refuse that up front; this is the
      predicate to refuse on.
    """
    return out is not None and bool(out.suffix) and not out.is_dir()


def bundle_output_path(network: NetworkConfig, created: str,
                       out: Path | None = None) -> Path:
    """Where this bundle lands. `out` may be a file or a directory."""
    name = f"{network.name}-{created}.tar"
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
    with no mmw installed — `sha256sum -c lab-….tar.sha256` is enough. That
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


def _far_nginx(settings: str, tls_files: dict[str, bytes]) -> dict[str, bytes]:
    """The far side's nginx config for a snapshot, with the root left as a
    placeholder (apply.py fills it in)."""
    tls = ((f"{ROOT_PLACEHOLDER}/{TLS_DIR}/mirror.crt",
            f"{ROOT_PLACEHOLDER}/{TLS_DIR}/mirror.key") if tls_files else None)
    return recorded_nginx_files(settings, ROOT_PLACEHOLDER, tls=tls)


_LAST = object()        # from_ref default: where the last bundle of this network went


def _resolve_from(pool: Pool, network: str, from_ref):
    from mmw.core.snapshots import find_snapshot
    if from_ref is None:
        return None                                  # full
    if from_ref is _LAST:
        log = pool.bundles(network)
        if log:
            return find_snapshot(pool, network, log[0].to_stamp)
        # Never bundled since snapshots existed: the newest imported bundle
        # manifest is where the far side was last sent.
        imported = [s for s in pool.snapshots(network) if s.kind == "imported"]
        return imported[0] if imported else None
    return find_snapshot(pool, network, from_ref)


def _free_target(network, name: str, out_path) -> Path:
    target = bundle_output_path(network, name, out_path)
    if is_explicit_file_out(out_path):
        if target.exists():
            raise ValueError(
                f"{target} already exists — refusing to overwrite an explicit --out target. "
                f"Remove it, or pass a directory to --out and let mmw name the bundle.")
        return target
    n = 2
    while target.exists():                      # the same pair bundled twice
        target = bundle_output_path(network, f"{name}.{n}", out_path)
        n += 1
    return target


def bundle_network(
    network: NetworkConfig,
    cfg: Config,
    mirror_dir: Path,
    out_path: Path | None = None,
    full: bool = False,
    progress=None,
    stop_event=None,
    mirror_start_callback=None,
    *,
    from_ref=_LAST,
    to_ref: str = "now",
) -> BundleResult:
    """One `.tar` that takes a far side from snapshot `from_ref` to
    snapshot `to_ref` — any two, forwards or backwards.

    - `to_ref`: a complete snapshot (stamp, label or id), or "now": a `bundle`
      snapshot of the network's current state is taken first.
    - `from_ref`: a snapshot (an imported, history-only one is fine — only its
      file list is needed), None for a full bundle, or by default where the
      last bundle of this network went. `full=True` is `from_ref=None`.

    Both ends are complete trees, so the delta is exact: send every path of
    the destination whose checksum differs from (or is absent in) the start;
    delete every path of the start the destination lacks. A full bundle sends
    everything and deletes every path any snapshot of this network ever had
    that the destination lacks — a far side of unknown state is repaired.
    The nginx config and TLS files are regenerated for the far side.
    """
    from mmw.core.snapshots import (
        find_snapshot, import_ledger, recorded_bases, take_snapshot,
    )

    if full:
        from_ref = None
    with Pool(mirror_dir) as pool:
        import_ledger(pool, mirror_dir, network.name)
        start = _resolve_from(pool, network.name, from_ref)
        if to_ref != "now":
            dest = find_snapshot(pool, network.name, to_ref)
            if not dest.complete:
                raise ValueError(
                    f"snapshot {dest.stamp} is history only (imported from a bundle made "
                    "before snapshots existed; its files may be gone) — it can be a "
                    "starting point, never a destination")
    du_warnings: list[str] = []
    if to_ref == "now":
        bundle_output_path(network, "x", out_path)   # destination check
        dest = take_snapshot(network, cfg, mirror_dir, kind="bundle", stop_event=stop_event,
                             warnings=du_warnings)
    if start is not None and start.id == dest.id:
        raise ValueError(f"from and to are the same snapshot ({dest.stamp}) — nothing to send")

    # Checked before any work: a certs network's bundle that cannot carry its
    # certificate must not quietly ship as HTTP-only after an hour of
    # archiving. (The private key rides along: that is what `certs` on a
    # bundled network means.)
    tls_files: dict[str, bytes] = {}
    if network.certs:
        tls_paths = servable_paths(network, cfg)
        if tls_paths is None:
            raise ValueError(
                f"network '{network.name}' has certs = true but no usable "
                f"signed certificate in {cert_dir(network, cfg)} — "
                f"run `mmw certs -n {network.name} --ca CA.crt --ca-key CA.key`"
            )
        tls_files = {"mirror.crt": tls_paths[0].read_bytes(),
                     "mirror.key": tls_paths[1].read_bytes()}

    name = dest.stamp if start is None else f"{start.stamp}-to-{dest.stamp}"
    target = _free_target(network, name, out_path)
    header = ManifestHeader(network=network.name, created=dest.stamp,
                            previous=start.stamp if start else None, full=start is None,
                            previous_label=start.label if start else None,
                            created_label=dest.label)
    result = BundleResult(network_name=network.name, out_path=target, created=dest.stamp,
                          previous=header.previous, full=header.full)
    result.dist_upgrade_warnings = du_warnings

    with Pool(mirror_dir, read_only=True) as pool:
        missing = pool.snapshot_missing(dest.id)
        if missing:
            raise ValueError(f"{missing} file(s) of snapshot {dest.stamp} are no longer in "
                             "the pool — it cannot be bundled to")
        prev = dict(pool.snapshot_files(start.id)) if start else {}
        if start is not None:
            # Snapshots never store the nginx config (it embeds a root), so the
            # start's is regenerated the same way: unchanged, it isn't resent;
            # gone, it is deleted by the ordinary rule below.
            prev.update({rel: hashlib.sha256(data).hexdigest()
                         for rel, data in _far_nginx(start.settings, tls_files).items()})
        sink = BundleSink(target, header, prev, apply_script_bytes(),
                          prune=network.prune, full=start is None,
                          progress=progress, tls_files=tls_files)
        bases = recorded_bases(dest.settings)
        dest_paths: set[str] = set()
        current_base, seen_bases = None, 0
        for i, (rel, checksum, pool_path, size) in enumerate(pool.snapshot_blob_rows(dest.id)):
            if stop_event is not None and i % 1000 == 0 and stop_event.is_set():
                break
            base = next((b for b in bases if rel.startswith(b + "/")), None)
            if base is not None and base != current_base and mirror_start_callback:
                current_base, seen_bases = base, seen_bases + 1
                mirror_start_callback(base, seen_bases, len(bases))
            dest_paths.add(rel)
            sink.link(Path(pool_path), rel, size, checksum, "pkg")
        if stop_event is not None and stop_event.is_set():
            result.own_errors.append("stopped before the bundle was written")
            return result

        for rel, data in _far_nginx(dest.settings, tls_files).items():
            sink.write(rel, data)
            dest_paths.add(rel)

        if network.prune:
            if start is not None:
                gone = (p for p in prev if p not in dest_paths)
            else:
                gone = (p for p in pool.network_snapshot_paths(network.name)
                        if p not in dest_paths)
            sink.deleted = sorted(set(gone))

    before_close = len(sink.errors)
    sink.close()
    result.own_errors.extend(sink.errors[before_close:])

    # The exact manifest shipped, kept beside the ledger's history.
    manifest_path = ledger_dir(mirror_dir, network.name) / f"{dest.stamp}{MANIFEST_SUFFIX}"
    write_manifest(manifest_path, sink.written_header or header, sink.manifest_rows)
    (target.parent / "apply.py").write_bytes(apply_script_bytes())
    result.checksum_path = _write_checksum_file(target)
    result.manifest_path = manifest_path
    result.sent = sink.sent
    result.sent_bytes = sink.sent_bytes
    result.deleted = len(sink.deleted)
    with Pool(mirror_dir) as pool:
        pool.record_bundle(network.name, header.previous, dest.stamp, str(target),
                           sink.sent, sink.sent_bytes, len(sink.deleted))
    return result
