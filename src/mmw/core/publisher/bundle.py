"""Bundle a network into one plain .tar for transfer across an airgap.

No compression: the payload is packages, already compressed, and zstd over
terabytes on one disk only made it slower. Compress the tar yourself if a
transfer medium wants it (the far side then needs it decompressed first).

Same pipeline as publish (`publisher/__init__.py:publish_network`): one
refs⋈packages enumeration per mirror_base, ordered pkg → meta → release, the
same dist-upgrade and key steps in the same order. Only the sink differs —
`PointsSink` instead of `TreeSink` — which is exactly the point: if a bundle
and a publish ever disagree about what a network contains, that is a sink bug,
not two independently-maintained pipelines drifting apart.

Deltas come from the on-disk manifest ledger (`manifest.py`), so a re-bundle a
month later carries what changed since the last one and nothing else.
"""
import hashlib
import re
from pathlib import Path

from mmw.core import disk_io
from mmw.core.format import fmt_size
from mmw.core.config import Config, NetworkConfig
from mmw.core.pool import KIND_META, KIND_PKG, Pool, holds_shared_pool_lock

from mmw.core.publisher.manifest import (
    MANIFEST_SUFFIX,
    ManifestHeader,
    ledger_dir,
    write_manifest,
)
from mmw.core.publisher.nginx import ROOT_PLACEHOLDER, recorded_nginx_files
from mmw.core.publisher.results import BundleResult
from mmw.core.publisher.sinks import TLS_DIR


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


def _write_checksum_file(target: Path, digest: str | None = None) -> Path:
    """Write `{target}.sha256` beside the bundle, in `sha256sum -c` format.

    The far side can then tell a bad *transfer* from a bad *bundle* in seconds,
    with no mmw installed — `sha256sum -c lab-….tar.sha256` is enough. That
    distinction matters because a tarball truncated on a member boundary still
    extracts cleanly, so without this the first sign of a bad copy is
    the far side's apply an hour later, or nothing at all.

    Written tmp-then-rename for the same reason the tarball and the manifest
    are: a half-written checksum file that happens to parse is worse than none.
    `digest` is the one taken while the tar was written; without it the
    file is read back.
    """
    digest = digest or disk_io.hash_file(target)
    dest = target.with_name(target.name + ".sha256")
    tmp = dest.with_name(dest.name + ".tmp")
    disk_io.ops.write_text(tmp, f"{digest}  {target.name}\n")
    disk_io.ops.replace(tmp, dest)
    return dest


def _far_nginx(settings: str, certs: bool, *, previous: bool = False) -> dict[str, bytes]:
    """Fixed container config, or the legacy previous-manifest form.

    the far side enables HTTPS once its certificate fits its key.
    """
    include = f"{ROOT_PLACEHOLDER}/{TLS_DIR}/https*.conf" if certs else None
    import json
    legacy = previous and not json.loads(settings or "{}").get("nginx_container", False)
    return recorded_nginx_files(settings, ROOT_PLACEHOLDER, tls_include=include, legacy=legacy)


class NothingToStandOn(ValueError):
    """No kept manifest or retained points support the requested baseline."""


_LAST = object()        # from_ref default: where the last bundle went


def _carried_keys(network, mirror_dir: Path) -> dict[str, bytes]:
    """The private parts of the keys `network` carries to its far side."""
    if not network.carry_keys:
        return {}
    from mmw.core.signing import Keyring
    keyring = Keyring(mirror_dir)
    try:
        return {name: keyring.private_armor(name).encode() for name in network.carry_keys}
    finally:
        keyring.stop_agent()


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


def resolve_since(network, mirror_dir: Path, since_date: str, kept_dirs=None, *, cfg=None, repos=None,
                  whole=None):
    """Read an explicit baseline, shared by transfer planning and its preview.

    This side's points as of the date, projected with the selection the far
    side is assumed to hold (`transfer_ranges.assumed_selection`): a kept
    bundle at or before the date says what it was sent; `--whole` what it lacks."""
    from mmw.core.points import resolve_when
    baseline, source, skipped = None, None, []
    from mmw.core.publisher.kept import state_at
    since = resolve_when(since_date)
    if cfg is not None:
        from mmw.core.transfer_ranges import (
            assumed_selection, kept_state, point_date, recorded_selection, repository_owned,
            retained_track_points)
        from mmw.core.repository_points import files_for_settings
        with Pool(mirror_dir, read_only=True) as pool:
            try:
                settings, current = recorded_selection(pool, network, mirror_dir, cfg, only=repos)
            except ValueError:
                current = {}
            if repository_owned(pool, current, network):
                known = pool.points_of(pid for ids in current.values() for pid in ids)
                selected = []
                for tid in {p.track for p in known.values()}:
                    candidates = [(point_date(pool, p, settings), p) for p in
                                  retained_track_points(pool, network.name, tid, known, settings)]
                    eligible = [(date, p) for date, p in candidates if date <= since]
                    if not eligible:
                        point = next(p for p in known.values() if p.track == tid)
                        if network.layer_for(pool.track_of_point(point.id).repo) is not None:
                            continue  # A layer first recorded after From needs its full payload.
                        raise NothingToStandOn(f'no retained repository point at or before {since_date} '
                                               '— choose a later From date or send everything')
                    selected.append(max(eligible, key=lambda item: (item[0], item[1].stamp, item[1].ident))[1])
                base, _ = assumed_selection(network, mirror_dir, settings, cfg=cfg, since=since,
                                            kept_dirs=kept_dirs, whole=whole)
                baseline = {cs for p in selected if p.held for cs in files_for_settings(pool, p.id, base).values()}
                kept = kept_state(network, mirror_dir, since, kept_dirs)
                if kept is not None and kept.settings is None:
                    baseline &= kept.checksums      # an older manifest names no selection: trust its files
                return since, baseline, f'repository points as of {since_date}', []
    dirs = [*([network.bundle_dir] if network.bundle_dir is not None else []),
            ledger_dir(mirror_dir, network.name), *(kept_dirs or [])]
    state = state_at(dirs, network.name, since, skipped=skipped)
    if state is not None:
        baseline = set(state.checksums)
        source = f"{Path(state.where).name} (its manifest)"
    else:
        with Pool(mirror_dir, read_only=True) as pool:
            from mmw.core.repository_points import files_for_settings, wire_draft
            settings = pool.settings_at(network.name, since) or pool.settings_at(network.name, "99999999T999999Z") or "{}"
            selected = []
            for track in pool.network_tracks(network.name):
                eligible = []
                for p in pool.network_points(network.name, track.id, stubs=True):
                    if p.legacy:
                        date = p.date
                    elif p.held:
                        files = files_for_settings(pool, p.id, settings)
                        date = wire_draft(pool, p.id, files, settings, p.stamp).date if files else None
                    else:
                        date = pool.network_slice_date(network.name, p.id)
                    if date is not None and date <= since:
                        eligible.append((date, p.stamp, p.ident, p))
                if eligible:
                    selected.append(max(eligible, key=lambda item: item[:3])[3])
            if selected and all(p.held for p in selected):
                baseline = {cs for p in selected for cs in files_for_settings(pool, p.id, settings).values()}
                source = f"the points of {since_date}"
        if baseline is None:
            raise NothingToStandOn(
                f"no state for {network.name} on {since_date}; searched "
                f"{', '.join(map(str, dirs))} and this network's retained points"
                + (f" ({'; '.join(skipped)})" if skipped else "")
                + " — send everything with --full")
    return since, baseline, source, skipped


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
    installer: bool = False,
    runtime_wheel: Path | None = None,
    fetcher=None,
    history: bool | None = None,
    full: bool = False,
    since: str | None = None,
    kept_dirs: list[Path] | None = None,
    until: str | None = None,
    whole: list[str] | None = None,
) -> BundleResult:
    """One bundle for the network's far side: each chosen repository's
    timeline points (newest, or with `history` every point since it was
    last bundled), carrying only files the network was never sent (`full`
    or `from_ref=None`: every file). See `_bundle_points`.

    `installer` writes mmw's installer (`mmw-<version>-<arch>.tar`, one per
    `runtime_arches`) beside the bundle once it is written — never inside
    it, and a failure to build one is only a warning. `runtime_wheel` is
    the mmw wheel to ship (default: this checkout's own build, or PyPI's
    for a release); `fetcher` does the downloads.
    """
    if until is not None and to_ref != "now":
        raise ValueError("--until cannot be combined with --to")
    if since is not None and (full or from_ref is not _LAST):
        raise ValueError("--since cannot be combined with --full or --from")
    if installer:
        if is_stdout(out_path) and not stage:
            raise ValueError("a bundle on stdout has nowhere to put mmw's installer — "
                             "write it on its own with `mmw installer --out DIR`")
        if runtime_wheel is not None:
            _check_wheel(runtime_wheel)             # before hours of bundling, not after
    return _bundle_points(network, cfg, mirror_dir, out_path, progress, stop_event,
                          mirror_start_callback=mirror_start_callback, from_ref=from_ref, to_ref=to_ref, stdout=stdout, repos=repos, stage=stage,
                          history=network.history if history is None else history,
                          full=full or from_ref is None, installer=installer,
                          runtime_wheel=runtime_wheel, fetcher=fetcher, since_date=since, kept_dirs=kept_dirs, until=until,
                          whole=whole)


def _bundle_points(network: NetworkConfig, cfg: Config, mirror_dir: Path, out_path, progress, stop_event, *,
                   mirror_start_callback=None,
                   from_ref, to_ref: str, stdout, repos: list[str] | None, stage: bool, history: bool,
                   full: bool, installer: bool, runtime_wheel, fetcher, since_date=None, kept_dirs=None, until=None,
                   whole=None) -> BundleResult:
    """An mmw network's bundle of timeline points: each chosen repository's
    current points (always listed — the far side retires what's missing),
    and with `history` every point first seen since the last bundle; a file
    goes only if this network was never sent it (`full`: every file). Both
    records are written only once the bundle is complete."""
    from mmw.core import points as pts
    from mmw.core.publisher import warm_for_publish
    from mmw.core.publisher.results import RepoSummary
    from mmw.core.publisher.sinks import PointsSink
    from mmw.core.repos import repository_label
    from mmw.core.snapshots import find_snapshot, recorded_repos
    from mmw.site.versionref import day

    from mmw.core.transfer_ranges import validate_dates
    validate_dates(since_date, until)
    warm_for_publish(mirror_dir, progress, stop_event)
    from mmw.core import snapshots as _snaps            # the one clock snapshots and bundles share
    stamp = _snaps.now_stamp()
    with Pool(mirror_dir) as pool:
        pts.migrate_sent(pool, network.name)
        since_by_repo, since = pts.history_since(pool, network.name)
        if isinstance(from_ref, str):           # a stamp, label or id: history from there, for all
            try:
                since = find_snapshot(pool, network.name, from_ref).stamp
            except LookupError:
                if not re.fullmatch(r"\d{8}T\d{6}Z", from_ref):
                    raise
                since = from_ref
            since_by_repo = {}
        elif from_ref is not None and from_ref is not _LAST and hasattr(from_ref, "stamp"):
            since, since_by_repo = from_ref.stamp, {}
        elif from_ref is None:
            since, since_by_repo = None, {}
    baseline, source, skipped = None, None, []
    if since_date is not None:
        since, baseline, source, skipped = resolve_since(network, mirror_dir, since_date, kept_dirs, cfg=cfg,
                                                         repos=repos, whole=whole)
        since_by_repo = {}
    result = BundleResult(network_name=network.name, created=stamp, previous=since, full=full)
    result.since_source = source
    result.warnings.extend(skipped)
    from mmw.core.transfer_ranges import recorded_selection, repository_owned, selected_tree
    detached, extra_files = False, {}
    if to_ref == 'now':
        with Pool(mirror_dir, read_only=True) as pool:
            from mmw.core.resolve import network_mirrors
            from mmw.core.repos import repository_key
            repos_now = {repository_key(u) for u in network_mirrors(network, cfg, mirror_dir)}
            chosen_now = repos_now if repos is None else repos_now.intersection(repos)
            # A live transfer refreshes any selected layer through the same
            # recording walk as a layer-only network. A dated transfer replays it.
            refresh_layers = until is None and any(network.layer_for(repo) for repo in chosen_now)
            if not refresh_layers and any(pool.repository_points(repo, stubs=True) for repo in repos_now):
                settings, current = recorded_selection(pool, network, mirror_dir, cfg, only=repos)
                detached = repository_owned(pool, current, network)
    if detached:
        bundle_output_path(network, 'x', out_path)
        with Pool(mirror_dir) as pool:
            _, settings, extra_files, notes, _ = selected_tree(
                pool, network, cfg, mirror_dir, settings, current,
                until=until, progress=progress, stop_event=stop_event)
        result.warnings.extend(notes)
    elif until is not None:
        from mmw.core.points import resolve_when
        resolve_when(until)  # Validate before reading or creating output.
        with Pool(mirror_dir, read_only=True) as pool:
            settings, current = recorded_selection(pool, network, mirror_dir, cfg)
    elif to_ref == "now":
        bundle_output_path(network, "x", out_path)               # destination check
        report = pts.record_network_points(network, cfg, mirror_dir, stamp=stamp, stop_event=stop_event)
        result.warnings.extend(report.warnings)
        settings = report.settings
        with Pool(mirror_dir) as pool:
            settings, current = recorded_selection(pool, network, mirror_dir, cfg)
    else:
        with Pool(mirror_dir) as pool:
            snap = find_snapshot(pool, network.name, to_ref)
            if not snap.complete:
                raise ValueError(
                    f"snapshot {snap.stamp} is history only (its files may be gone) — it can be a "
                    "starting point, never a destination")
            settings = snap.settings
            current = {}
            for pid in pool.snapshot_point_ids(snap.id):
                t = pool.track_of_point(pid)
                if t is not None:
                    current.setdefault(t.repo, []).append(pid)
        stamp = snap.stamp
        result.created = stamp
    repo_bases = recorded_repos(settings)
    chosen = [r for r in sorted(current) if not r.startswith("network:") and (repos is None or r in repos)]
    if repos is not None:
        unknown = [r for r in repos if r not in current]
        if unknown:
            raise LookupError(f"unknown repository {unknown[0]!r} — this network has: "
                              f"{', '.join(r for r in sorted(current) if not r.startswith('network:'))}")
    net_repo = f"network:{network.name}"

    with Pool(mirror_dir, read_only=True) as pool:
        from mmw.core.repository_points import files_for_settings
        from mmw.site.tracks import content_ident
        sent_points = {p for p in pool.sent_point_ids(network.name)
                       if pool.sent_slice(network.name, p) in (None, content_ident(files_for_settings(pool, p, settings)))}
        tracks = {t.id: t for t in pool.network_tracks(network.name)}
        from mmw.core.transfer_ranges import point_date, select_range
        current, listing, known = select_range(
            pool, network.name, settings, current, until=until, history=history,
            since=since, since_by_repo=since_by_repo if not full else {},
            explicit_since=since_date is not None or from_ref not in (_LAST, None),
            sent_points=sent_points if not full else (), repos=chosen, warnings=result.warnings, full=full)
        files = {pid: files_for_settings(pool, pid, settings) for ids in listing.values() for pid in ids}
        net_files: dict[str, str] = {}
        for pid in current.get(net_repo, []):
            net_files.update(pool.point_files(pid))
        net_files.update(extra_files)
        from mmw.core.publisher.deployment import RETIRED
        for name in RETIRED:                         # an old snapshot's far-side scripts never travel
            net_files.pop(name, None)
        every = {c for f in files.values() for c in f.values()} | set(net_files.values())
        info = pool.blob_info(every)
        # Generated network files too: sent before, they don't go again.
        generated = dict(_far_nginx(settings, network.certs))
        gen_sums = {hashlib.sha256(d).hexdigest() for d in generated.values()}
        sent = baseline if baseline is not None else (set() if full else pool.sent_checksums(network.name, every | gen_sums))
        from mmw.core.transfer_ranges import assumed_selection, range_baseline
        base, result.whole = (settings, []) if full else assumed_selection(
            network, mirror_dir, settings, cfg=cfg, since=since if since_date is not None else None,
            kept_dirs=kept_dirs, whole=whole)
        interval_base = range_baseline(pool, network, settings, current, since=since_date, full=full, listing=listing,
                                       base_settings=None if base is settings else base)
        if interval_base is not None:
            # Both are what the far side is assumed to hold: trust only what both say.
            sent = interval_base if baseline is None else interval_base & baseline
        missing = sorted(c for c in every if c not in info)
        if missing:
            raise ValueError(f"{len(missing)} file(s) of {network.name}'s points are no longer in the pool "
                             f"(first: {missing[0][:12]}…) — sync again, or `mmw verify --repair`")
        header_points: dict = {}
        sent_dates = pool.sent_dates_by_track(network.name)
        # A suite gone since the last bundle listed it is news too: the far side retires it.
        listed_tracks = {repo: {known[p].track for p in ids if p in known} for repo, ids in listing.items()}
        before = pool.listed_tracks(network.name)
        # A repository an earlier bundle listed that the network no longer
        # brings: named, so the far side stops serving it. Only a whole,
        # current transfer can tell (a --repo or dated one lists a part).
        left = (sorted(r for r, was in before.items() if was and r not in listing and not r.startswith("network:"))
                if repos is None and until is None and to_ref == "now" else [])
        retiring = bool(left) or any((before.get(repo) if repo in before else
                        {t for t in sent_dates if t in tracks and tracks[t].repo == repo}) - now
                       for repo, now in listed_tracks.items())
        summaries = []
        for repo, ids in listing.items():
            label = repository_label(repo, cfg)
            entries = []
            for pid in ids:
                p = known[pid]
                t = tracks.get(p.track) or pool.track_of_point(pid)
                from mmw.core.repository_points import wire_draft
                wire = wire_draft(pool, pid, files[pid], settings, p.stamp)
                entries.append({"track": t.name, "ident": wire.ident, "date": wire.date,
                                "stamp": pool.network_point_stamp(network.name, pid) or p.stamp,
                                "snap_label": None,
                                "fields": {k: v for k, v in wire.fields.items() if k in ("Suite", "Codename")}})
                from mmw.core.point_check import check_suite
                from mmw.core.points import _reader
                problems = check_suite(files[pid], t.name, _reader(pool, files[pid])) if t.name != 'network' else []
                if problems:
                    entries[-1]['problems'] = problems
            header_points[repo] = {"label": label, "bases": repo_bases.get(repo, []), "points": entries}
            suites = []
            for t in sorted({known[pid].track for pid in current[repo]}):
                track = tracks.get(t) or pool.track_of_point(next(p for p in current[repo] if known[p].track == t))
                now_pt = max(point_date(pool, known[pid], settings) for pid in current[repo] if known[pid].track == t)
                before = sent_dates.get(t)
                suites.append((track.suite or (track.name.rsplit("/", 1)[-1] if track.name else "files"),
                               day(before) if before else None, day(now_pt)))
            summaries.append(RepoSummary(label, suites, repo))
    result.repos = summaries

    header = ManifestHeader(network=network.name, created=stamp, previous=since, full=full,
                            format=3, selection=True, points=header_points, settings=settings,
                            left=left or None)
    signing_files = _carried_keys(network, mirror_dir)
    name = stamp if since is None else f"{since}-to-{stamp}"
    target = _free_stage_dir(network, name, out_path) if stage else _free_target(network, name, out_path)
    result.out_path = target
    streaming = is_stdout(out_path) and not stage
    hashing = None
    writer = None
    staged: list = []
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
    sink = PointsSink(target if not streaming else Path(f"{network.name}-{name}.tar"), header, sent=sent,
                      progress=progress, stop_event=stop_event, writer=writer, signing_files=signing_files,
                      scratch_dir=ledger_dir(mirror_dir, network.name) if streaming or stage else None)
    for i, (repo, ids) in enumerate(listing.items(), 1):
        if mirror_start_callback:                    # which repository is going in, for the progress page
            mirror_start_callback((repo_bases.get(repo) or [repo])[0], i, len(listing))
        for k, pid in enumerate(ids):
            for rel, cs in sorted(files[pid].items()):
                src, size = info[cs]
                sink.link_point(src, rel, size, cs, KIND_PKG, repo, k)
    for rel, cs in sorted(net_files.items()):
        src, size = info[cs]
        sink.link(src, rel, size, cs, KIND_META)
    for rel, data in sorted(generated.items()):
        sink.write(rel, data)
    if sink.sent == 0 and not retiring and all(pid in sent_points for ids in listing.values() for pid in ids):
        raise ValueError("nothing to send — every chosen repository is already there")
    if stage and not _same_filesystem(mirror_dir, target.parent):
        warning = (f"{target.parent} is on another filesystem than the pool — "
                   f"{fmt_size(sink.sent_bytes)} will be copied, not hardlinked")
        result.warnings.append(warning)
        if progress:
            progress("", f"warning:{warning}")
    try:
        sink.close()
    except InterruptedError:
        result.own_errors.append("stopped before the bundle was written")
        return result
    finally:
        result.timings.update(sink.job.stats.timings())

    manifest_path = ledger_dir(mirror_dir, network.name) / f"{stamp}{MANIFEST_SUFFIX}"
    write_manifest(manifest_path, sink.written_header or header, sink.tagged_rows())
    result.manifest_path = manifest_path
    if streaming:
        result.stream_sha256 = hashing.hexdigest()
    elif stage:
        copied = staged[-1] if staged else None
        if copied is not None and copied.copied_files and not any("another filesystem" in w
                                                                 for w in result.warnings):
            result.warnings.append(f"{copied.copied_files} file(s) could not be hardlinked and were "
                                   f"copied ({fmt_size(copied.copied_bytes)})")
    else:
        result.checksum_path = _write_checksum_file(target, sink.archive_sha256)
    result.sent, result.sent_bytes = sink.sent, sink.sent_bytes
    with Pool(mirror_dir) as pool:
        listed = {pid for ids in listing.values() for pid in ids} | set(current.get(net_repo, []))
        pool.record_sent(network.name, listed, sink.sent_checksums | (baseline or set()), stamp,
                         slices={pid: content_ident(f) for pid, f in files.items()},
                         targets={known[pid].track: (repo, point_date(pool, known[pid], settings))
                                  for repo in listing for pid in current[repo]})
        pool.set_listed_tracks(network.name, {**listed_tracks, **{r: set() for r in left}})
        pool.record_bundle(network.name, since, stamp, str(target), sink.sent, sink.sent_bytes, 0,
                           kind="stage" if stage else "stream" if streaming else "bundle")
    if installer and not (stop_event is not None and stop_event.is_set()):
        _write_installers(result, network, mirror_dir, Path(target).parent, runtime_wheel, fetcher,
                          progress, stop_event)
    return result


def _write_installers(result, network, mirror_dir: Path, out_dir: Path, wheel, fetcher, progress,
                      stop_event) -> None:
    """mmw's installers beside a written bundle. A failure is a warning: the
    bundle is complete without them."""
    from mmw.core import runtime as rt
    from mmw.core.installer import write_installers
    cache = Path(mirror_dir) / "runtime"
    try:
        result.installers = write_installers(
            out_dir, arches=sorted(network.runtime_arches), python=network.runtime_python, cache=cache,
            fetcher=fetcher or rt.default_fetcher(stop_event=stop_event),
            wheel=rt.resolve_wheel(wheel, cache / "build", stop_event=stop_event),
            progress=(lambda m: progress("", f"runtime:{m}")) if progress else None, stop_event=stop_event)
    except (rt.RuntimeUnavailable, OSError, InterruptedError, ValueError) as e:
        result.warnings.append(f"mmw installer not written ({e})")
        return
    if result.installers:
        from mmw.core.installer import _version
        with Pool(mirror_dir) as pool:            # so the next transfer knows this mmw went across
            pool.mark_installer_sent(network.name, {"mmw": _version(wheel)})


def _check_wheel(wheel: Path) -> None:
    from packaging.utils import InvalidWheelFilename, parse_wheel_filename
    try:
        parse_wheel_filename(Path(wheel).name)
    except InvalidWheelFilename:
        raise ValueError(f"--runtime-wheel {wheel}: isn't a wheel file name "
                         "(mirror_mirror_on_the_wall-VERSION-py3-none-any.whl)") from None
    if not disk_io.ops.is_file(wheel):
        raise ValueError(f"--runtime-wheel {wheel}: no such wheel")


def stage_network(network: NetworkConfig, cfg: Config, mirror_dir: Path, out_dir: Path | None = None,
                  progress=None, stop_event=None, mirror_start_callback=None, *,
                  from_ref=_LAST, to_ref: str = "now", repos: list[str] | None = None,
                  installer: bool = False, runtime_wheel: Path | None = None, fetcher=None,
                  history: bool | None = None, full: bool = False,
                  since: str | None = None, kept_dirs: list[Path] | None = None,
                  until: str | None = None, whole: list[str] | None = None) -> BundleResult:
    """`mmw stage`: the bundle `bundle_network` would write, laid out as a
    directory `{out_dir or bundle_dir}/{network}-{name}/` of hardlinks to the
    pool instead of a tar. For mmw networks only."""
    return bundle_network(network, cfg, mirror_dir, out_dir, progress, stop_event, mirror_start_callback,
                          from_ref=None if full else from_ref, to_ref=to_ref, repos=repos, stage=True,
                          installer=installer, runtime_wheel=runtime_wheel, fetcher=fetcher, history=history,
                          since=since, kept_dirs=kept_dirs, until=until, whole=whole)
