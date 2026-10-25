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

from mmw.core import disk_io
from mmw.core.format import fmt_size
from mmw.core.config import Config, NetworkConfig
from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool, holds_shared_pool_lock

from mmw.core.publisher.manifest import (
    MANIFEST_SUFFIX,
    ManifestHeader,
    ledger_dir,
    write_manifest,
)
from mmw.core.certs import bundle_tls_files
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
    return out is not None and bool(out.suffix) and not disk_io.ops.is_dir(out)


STDOUT = Path("-")


def is_stdout(out: Path | None) -> bool:
    """`--out -`: the bundle goes to stdout, not a file."""
    return out is not None and str(out) == "-"


def bundle_output_path(network: NetworkConfig, created: str,
                       out: Path | None = None) -> Path:
    """Where this bundle lands. `out` may be a file or a directory."""
    name = f"{network.name}-{created}.tar"
    if is_stdout(out):
        return STDOUT
    if out is not None:
        return out if is_explicit_file_out(out) else out / name
    if network.bundle_dir is None:
        raise ValueError(
            f"network '{network.name}' has no bundle destination — "
            f"set bundle_dir in mmw.toml or pass --out"
        )
    return network.bundle_dir / name



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
    digest = disk_io.hash_file(target)
    dest = target.with_name(target.name + ".sha256")
    tmp = dest.with_name(dest.name + ".tmp")
    disk_io.ops.write_text(tmp, f"{digest}  {target.name}\n")
    disk_io.ops.replace(tmp, dest)
    return dest


def _far_nginx(settings: str, certs: bool, *, previous: bool = False) -> dict[str, bytes]:
    """Fixed container config, or the legacy previous-manifest form.

    apply.py enables HTTPS after validating the far-side certificate/key.
    """
    include = f"{ROOT_PLACEHOLDER}/{TLS_DIR}/https*.conf" if certs else None
    import json
    legacy = previous and not json.loads(settings or "{}").get("nginx_container", False)
    return recorded_nginx_files(settings, ROOT_PLACEHOLDER, tls_include=include, legacy=legacy)


from mmw.core.publisher.repo_delta import _LAST  # noqa: E402 — from_ref default: where the last bundle went


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


def _same_filesystem(a: Path, b: Path) -> bool:
    """Whether hardlinks from `a` can land under `b` (the nearest existing
    parent of `b` is what is checked)."""
    while not disk_io.ops.exists(b) and b != b.parent:
        b = b.parent
    return disk_io.ops.stat(a).st_dev == disk_io.ops.stat(b).st_dev


def _free_stage_dir(network, name: str, out_dir: Path | None) -> Path:
    base = out_dir if out_dir is not None else network.bundle_dir
    if base is None:
        raise ValueError(f"network '{network.name}' has no bundle destination — "
                         "set bundle_dir in mmw.toml or pass --out")
    target, n = base / f"{network.name}-{name}", 2
    while disk_io.ops.exists(target):
        target = base / f"{network.name}-{name}.{n}"
        n += 1
    return target


def _free_target(network, name: str, out_path) -> Path:
    target = bundle_output_path(network, name, out_path)
    if is_stdout(out_path):
        return target
    if is_explicit_file_out(out_path):
        if disk_io.ops.exists(target):
            raise ValueError(
                f"{target} already exists — refusing to overwrite an explicit --out target. "
                f"Remove it, or pass a directory to --out and let mmw name the bundle.")
        return target
    n = 2
    while disk_io.ops.exists(target):                      # the same pair bundled twice
        target = bundle_output_path(network, f"{name}.{n}", out_path)
        n += 1
    return target


@holds_shared_pool_lock
def bundle_network(
    network: NetworkConfig,
    cfg: Config,
    mirror_dir: Path,
    out_path: Path | None = None,
    progress=None,
    stop_event=None,
    mirror_start_callback=None,
    *,
    from_ref=_LAST,
    to_ref: str = "now",
    stdout=None,
    repos: list[str] | None = None,
    stage: bool = False,
    runtime: bool = True,
    runtime_wheel: Path | None = None,
    fetcher=None,
) -> BundleResult:
    """One `.tar` that takes a far side from snapshot `from_ref` to
    snapshot `to_ref` — any two, forwards or backwards.

    - `to_ref`: a complete snapshot (stamp, label or id), or "now": a `bundle`
      snapshot of the network's current state is taken first.
    - `from_ref`: a snapshot (an imported, history-only one is fine — only its
      file list is needed), None for a full bundle, or by default where the
      last bundle of this network went.

    Both ends are complete trees, so the delta is exact: send every path of
    the destination whose checksum differs from (or is absent in) the start;
    delete every path of the start the destination lacks. A full bundle sends
    everything and deletes every path any snapshot of this network ever had
    that the destination lacks — a far side of unknown state is repaired.
    The nginx config and TLS files are regenerated for the far side.

    An mmw network's bundle carries mmw's own runtime (`.mmw/runtime/`) when
    it starts from nothing, or when the mmw version or the runtime settings differ from
    the last runtime sent — unless `runtime` is False. `runtime_wheel` is a
    local mmw wheel for a version PyPI lacks; `fetcher` does the downloads.
    """
    from mmw.core.snapshots import (
        ensure_repo_versions, find_snapshot, import_ledger, recorded_bases, take_snapshot,
    )

    mmw_far_side = network.far_side == "mmw"
    if repos is not None and not mmw_far_side:
        from mmw.core.far_side import require_mmw
        require_mmw(network, "partial bundles")

    from mmw.core.publisher import warm_for_publish
    warm_for_publish(mirror_dir, progress, stop_event)
    with Pool(mirror_dir) as pool:
        import_ledger(pool, mirror_dir, network.name)
        ensure_repo_versions(pool, network.name)
        start = _resolve_from(pool, network.name, from_ref)
        # mmw's runtime: built (or taken from the cache) before anything is
        # written — no snapshot, no bundle, no ledger row if a download
        # fails. Carried from nothing, or when what was last sent differs.
        shipped_runtime, runtime_members = None, {}
        if mmw_far_side and runtime:
            from mmw.core import runtime as rt
            want = rt.wanted(network, runtime_wheel)
            if start is None or pool.runtime_sent(network.name) != want:
                runtime_members = rt.runtime_files(
                    network, cfg, mirror_dir, fetcher=fetcher or rt.default_fetcher(stop_event=stop_event),
                    wheel=runtime_wheel, progress=progress, stop_event=stop_event)
                shipped_runtime = want
        if to_ref != "now":
            dest = find_snapshot(pool, network.name, to_ref)
            if not dest.complete:
                raise ValueError(
                    f"snapshot {dest.stamp} is history only (imported from a bundle made "
                    "before snapshots existed; its files may be gone) — it can be a "
                    "starting point, never a destination")
    snap_warnings: list[str] = []
    if to_ref == "now":
        bundle_output_path(network, "x", out_path)   # destination check
        dest = take_snapshot(network, cfg, mirror_dir, kind="bundle", stop_event=stop_event,
                             warnings=snap_warnings)
    # An mmw network's default start is per repository: the last bundle's
    # snapshot may still hold repositories it did not carry, so "the same
    # snapshot" is for the delta planner to judge, repository by repository.
    if start is not None and start.id == dest.id and not (mmw_far_side and from_ref is _LAST):
        raise ValueError(f"from and to are the same snapshot ({dest.stamp}) — nothing to send")

    # A certs network's key and signing request ride every bundle; the CA
    # lives on the far side, which signs there (sign.py) — nothing has to come
    # back. A certificate signed on this side rides along too.
    # A far side running mmw makes its own keys (`mmw site certs`): an mmw
    # network's bundles never put a private key on the transfer media.
    tls_files = bundle_tls_files(network, cfg) if network.certs and not mmw_far_side else {}

    name = dest.stamp if start is None else f"{start.stamp}-to-{dest.stamp}"
    target = _free_stage_dir(network, name, out_path) if stage else _free_target(network, name, out_path)
    header = ManifestHeader(network=network.name, created=dest.stamp,
                            previous=start.stamp if start else None, full=start is None,
                            previous_label=start.label if start else None,
                            created_label=dest.label)
    result = BundleResult(network_name=network.name, out_path=target, created=dest.stamp,
                          previous=header.previous, full=header.full)
    result.warnings = snap_warnings

    with Pool(mirror_dir, read_only=True) as pool:
        missing = pool.snapshot_missing(dest.id)
        if missing:
            raise ValueError(f"{missing} file(s) of snapshot {dest.stamp} are no longer in "
                             "the pool — it cannot be bundled to")
        deltas, included, dest_repo_bases = [], None, {}
        if mmw_far_side:
            from dataclasses import replace
            from mmw.core.publisher.repo_delta import plan_repo_deltas
            from mmw.core.repos import attribute
            from mmw.core.snapshots import recorded_repos, repo_version_files
            explicit = find_snapshot(pool, network.name, from_ref) if isinstance(from_ref, str) else from_ref
            deltas, delta_warnings = plan_repo_deltas(pool, network, cfg, dest, repos=repos, from_ref=explicit)
            result.warnings.extend(delta_warnings)
            result.repos = deltas
            included = {d.repo for d in deltas}
            dest_repo_bases = recorded_repos(dest.settings)
            header = replace(header, format=2, full=all(d.start is None for d in deltas),
                             settings=dest.settings, repos={
                d.repo: {"label": d.label, "from": d.start.number if d.start else None,
                         "to": d.dest.number, "digest": d.dest.digest, "bases": d.bases}
                for d in deltas})
            result.full = header.full
            # What the far side has: the network-level files of where the
            # last bundle went, and each repository's own starting version.
            prev = {}
            if start is not None:
                start_bases = recorded_repos(start.settings)
                prev = {p: c for p, c in pool.snapshot_files(start.id) if attribute(p, start_bases) is None}
            for d in deltas:
                if d.start is not None:
                    prev.update(repo_version_files(pool, d.start))
        else:
            prev = dict(pool.snapshot_files(start.id)) if start else {}
        if start is not None:
            # Generated setup is reconstructed from the start's recorded
            # format, including the old host-root form during upgrades.
            prev.update({rel: hashlib.sha256(data).hexdigest()
                         for rel, data in _far_nginx(start.settings, network.certs, previous=True).items()})
            import json
            if json.loads(start.settings or "{}").get("nginx_container") != 2:
                # Older deployments used host networking (or host roots).
                # Always ship their replacement setup in the next delta.
                for rel in prev:
                    if rel.startswith("nginx/") and not rel.endswith(".tar"):
                        prev[rel] = ""
        streaming = is_stdout(out_path) and not stage
        hashing = None
        writer = None
        staged = []                         # the DirMembers, once made
        if stage:
            from mmw.core.publisher.members import DirMembers

            def writer():
                staged.append(DirMembers(target))
                return staged[-1]
        if streaming:
            import sys
            from mmw.core.publisher.members import HashingWriter, TarMembers
            hashing = HashingWriter(stdout if stdout is not None else sys.stdout.buffer)
            writer = lambda: TarMembers(hashing)  # noqa: E731
        row_repo = (lambda rel: attribute(rel, dest_repo_bases)) if mmw_far_side else None
        sink = BundleSink(target if not streaming else Path(f"{network.name}-{name}.tar"), header, prev,
                          apply_script_bytes(), prune=network.prune and not mmw_far_side,
                          full=header.full if mmw_far_side else start is None, row_repo=row_repo,
                          progress=progress, tls_files=tls_files, stop_event=stop_event,
                          writer=writer, runtime_files=runtime_members,
                          scratch_dir=ledger_dir(mirror_dir, network.name) if streaming or stage else None)
        bases = recorded_bases(dest.settings)
        dest_paths: set[str] = set()
        current_base, seen_bases = None, 0
        # Packages as the rows stream past; indexes, then release files after
        # — the order publish uses, so a far side mid-apply never serves an
        # index naming a file not there yet.
        from mmw.core.publisher import replay_kind
        deferred: dict[str, list] = {KIND_META: [], KIND_RELEASE: []}
        for i, (rel, checksum, pool_path, size) in enumerate(pool.snapshot_blob_rows(dest.id)):
            if stop_event is not None and i % 1000 == 0 and stop_event.is_set():
                break
            base = next((b for b in bases if rel.startswith(b + "/")), None)
            if base is not None and base != current_base and mirror_start_callback:
                current_base, seen_bases = base, seen_bases + 1
                mirror_start_callback(base, seen_bases, len(bases))
            if included is not None:
                repo = attribute(rel, dest_repo_bases)
                if repo is not None and repo not in included:
                    continue                    # not in this bundle: the far side keeps its own
            dest_paths.add(rel)
            kind = replay_kind(rel, base)
            if kind == KIND_PKG:
                sink.link(Path(pool_path), rel, size, checksum, kind)
            else:
                deferred[kind].append((rel, checksum, pool_path, size))
        for kind in (KIND_META, KIND_RELEASE):
            for rel, checksum, pool_path, size in deferred[kind]:
                sink.link(Path(pool_path), rel, size, checksum, kind)
        if stop_event is not None and stop_event.is_set():
            result.own_errors.append("stopped before the bundle was written")
            return result

        for rel, data in _far_nginx(dest.settings, network.certs).items():
            sink.write(rel, data)
            dest_paths.add(rel)
        from mmw.core.publisher.deployment import write_scripts
        dest_paths |= write_scripts(sink, network.certs, existing=dest_paths)

        if network.prune and not mmw_far_side:
            if start is not None:
                gone = (p for p in prev if p not in dest_paths)
            else:
                gone = (p for p in pool.network_snapshot_paths(network.name)
                        if p not in dest_paths)
            sink.deleted = sorted(set(gone))

    if stage and not _same_filesystem(mirror_dir, target.parent):
        warning = (f"{target.parent} is on another filesystem than the pool — "
                   f"{fmt_size(sink.sent_bytes)} will be copied, not hardlinked")
        result.warnings.append(warning)
        if progress:
            progress("", f"warning:{warning}")
    before_close = len(sink.errors)
    try:
        sink.close()
    except InterruptedError:
        result.own_errors.append("stopped before the bundle was written")
        return result
    finally:
        result.timings.update(sink.job.stats.timings())
    result.own_errors.extend(sink.errors[before_close:])

    # The exact manifest shipped, kept beside the ledger's history.
    manifest_path = ledger_dir(mirror_dir, network.name) / f"{dest.stamp}{MANIFEST_SUFFIX}"
    write_manifest(manifest_path, sink.written_header or header, sink.tagged_rows())
    if streaming:
        result.stream_sha256 = hashing.hexdigest()
    elif stage:
        copied = staged[-1] if staged else None
        if copied is not None and copied.copied_files and not any("another filesystem" in w
                                                                 for w in result.warnings):
            result.warnings.append(f"{copied.copied_files} file(s) could not be hardlinked and were "
                                   f"copied ({fmt_size(copied.copied_bytes)})")
    else:
        disk_io.ops.write_bytes(target.parent / "apply.py", apply_script_bytes())
        if tls_files:
            for script in ("sign.py", "make-ca.py"):
                disk_io.ops.write_bytes(target.parent / script, tls_files[script])
        result.checksum_path = _write_checksum_file(target)
    result.manifest_path = manifest_path
    result.sent = sink.sent
    result.sent_bytes = sink.sent_bytes
    result.deleted = len(sink.deleted)
    with Pool(mirror_dir) as pool:
        # Per repository: the version the far side had (None: sent in full,
        # or new to it) and the one it has now.
        if mmw_far_side:
            sent_repos = {d.repo: (d.start.id if d.start else None, d.dest.id) for d in deltas}
        else:
            before = pool.snapshot_repo_versions(start.id) if start is not None else {}
            sent_repos = {repo: (before[repo].id if repo in before else None, v.id)
                          for repo, v in pool.snapshot_repo_versions(dest.id).items()}
        pool.record_bundle(network.name, header.previous, dest.stamp, str(target),
                           sink.sent, sink.sent_bytes, len(sink.deleted),
                           kind="stage" if stage else "stream" if streaming else "bundle",
                           repos=sent_repos, runtime=shipped_runtime)
    result.runtime = shipped_runtime
    return result


def stage_network(network: NetworkConfig, cfg: Config, mirror_dir: Path, out_dir: Path | None = None,
                  progress=None, stop_event=None, mirror_start_callback=None, *,
                  from_ref=_LAST, to_ref: str = "now", repos: list[str] | None = None,
                  runtime: bool = True, runtime_wheel: Path | None = None, fetcher=None) -> BundleResult:
    """`mmw stage`: the bundle `bundle_network` would write, laid out as a
    directory `{out_dir or bundle_dir}/{network}-{name}/` of hardlinks to the
    pool instead of a tar. For mmw networks only."""
    from mmw.core.far_side import require_mmw
    require_mmw(network, "mmw stage")
    return bundle_network(network, cfg, mirror_dir, out_dir, progress, stop_event, mirror_start_callback,
                          from_ref=from_ref, to_ref=to_ref, repos=repos, stage=True,
                          runtime=runtime, runtime_wheel=runtime_wheel, fetcher=fetcher)
