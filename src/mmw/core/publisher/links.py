"""Hardlink publishing: materialize a network tree from the pool DB.

The destination is built in trust order — packages, then index files (with
their by-hash links), then release files LAST, then stale-file pruning — so a
client hitting a live-served mirror mid-publish always sees a release file
whose referenced files exist.

Writing itself goes through a `Sink` (`sinks.py`): this module owns
enumeration, ordering and accounting, not filesystem writes.
"""
import posixpath
from pathlib import Path

from mmw.core import disk_io
from mmw.core.config import MirrorConfig
from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool

from mmw.core.publisher.results import MirrorPublishResult
from mmw.core.publisher.sinks import _prune_by_hash_dir  # noqa: F401 (re-export)
from mmw.core.disk_io.ops import (  # noqa: F401 (re-export until callers move)
    atomic_write, hardlink_or_copy, same_file, stage_path,
)

# Hardlinks are created in chunks so progress flows and stop is honored even
# when the phase is seek-bound on a cold inode cache.
_LINK_CHUNK = 2000


# refs whose repo_path starts with this are network-root-relative
# (changelogs.ubuntu.com meta-release files), not part of the mirror_base tree.
ROOT_RELATIVE_PREFIX = "changelogs.ubuntu.com/"


def is_package_file(rel: Path) -> bool:
    """True for package payload files that are tracked in the pool."""
    parts = rel.parts
    # APT: pool/<component>/<letter>/<package>/<file>
    if parts and parts[0] == "pool":
        return True
    return False


# Kept only so that files which already got a by-hash companion keep one;
# KIND_META rows no longer consult it. See `_wants_by_hash`.
_LEGACY_BY_HASH_PREFIXES = ("Packages", "Sources", "Release", "InRelease", "Translation-")


def _wants_by_hash(repo_path: str, kind: str = KIND_META) -> bool:
    """Should this file also be published under `by-hash/SHA256/<checksum>`?

    Every `KIND_META` row is an index file listed in the repository's own
    `Release`, and we mirror that `Release` byte for byte — including its
    `Acquire-By-Hash: yes`. So the mirror advertises by-hash for everything
    the Release lists, and must therefore provide it for everything the
    Release lists.

    It used to be a five-name allowlist (`Packages`, `Sources`, `Release`,
    `InRelease`, `Translation-`), which covers none of `Contents-*.gz`,
    `cnf/Commands-*`, `dep11/Components-*.yml.gz` or `dep11/icons-*.tar.gz`.
    A client with `Acquire::By-Hash "force"` fails outright on those; an
    ordinary client pays a round of 404s before falling back to the canonical
    path. Either way the mirror was promising a capability it only partly
    delivered. The cost of the extra links is one hardlink each — the pool key
    already *is* the sha256, so there is nothing to hash.
    """
    # Additive, deliberately: every KIND_META row qualifies, and anything that
    # qualified under the old name allowlist still does. Release files are not
    # listed inside their own Release block so apt never asks for them by
    # hash — but they were being published that way already, and quietly
    # *removing* a path a far side might hold is not what this fix is for.
    if kind == KIND_META:
        return bool(Path(repo_path).name)
    name = Path(repo_path).name
    return any(name.startswith(p) for p in _LEGACY_BY_HASH_PREFIXES)


def _link_rows(
    rows: list[tuple[str, str, int, str, str]],
    base: str,
    sink,
    result: MirrorPublishResult,
    progress,
    stop_event,
    by_hash: bool,
) -> bool:
    """Push (repo_path, pool_path, …) rows through `sink`, chunked.

    Returns False if interrupted by the stop event.
    """
    for i in range(0, len(rows), _LINK_CHUNK):
        if stop_event and stop_event.is_set():
            return False
        for run in _trust_runs(rows[i:i + _LINK_CHUNK]):
            items = [(Path(r[1]), f"{base}/{r[0]}", r[2], r[4], r[3]) for r in run]
            for (repo_path, pool_path, size, kind, checksum), (status, added) in zip(
                    run, _place_run(sink, items, pooled=True)):
                _settle_row(status, added, repo_path, pool_path, size, kind, checksum,
                            base, sink, result, progress, by_hash)
    return True


def _place_run(sink, items, *, pooled: bool = False):
    """(status, errors this item added) for each (src, rel, size, checksum,
    kind) of one trust run, in order — `sink.link_many` when the sink has one
    (its filesystem work on the job's write lane), else `sink.link` one at a
    time. Fewer pairs than items means a stop ended the run. `pooled`: every
    src is a pool blob (`mirror_files` rows), which a sink with `link_pooled`
    records without asking the pool again."""
    many = getattr(sink, "link_many", None)
    if many is not None:
        for status, error in many(items):
            yield status, [error] if error else []
        return
    link = _linker(sink, pooled)
    for src, rel, size, checksum, kind in items:
        # Capture the length before the call so an "error" status can
        # attribute exactly the entries THIS call added, not just assume the
        # last one is ours.
        before = len(sink.errors)
        status = link(src, rel, size, checksum, kind)
        yield status, sink.errors[before:]


def _linker(sink, pooled: bool):
    return getattr(sink, "link_pooled", sink.link) if pooled else sink.link


def _trust_runs(rows):
    """Consecutive rows a sink may place in any order: a run never crosses
    into the release files, which go last. `publish_group` already calls once
    per kind; `publish_by_host` interleaves each file with its `.metadata`
    (pkg, meta, pkg, …), so splitting on every kind change made runs of one."""
    run: list = []
    for row in rows:
        if run and (row[3] == KIND_RELEASE) != (run[-1][3] == KIND_RELEASE):
            yield run
            run = []
        run.append(row)
    if run:
        yield run


def _settle_row(status, added, repo_path, pool_path, size, kind, checksum,
                base, sink, result, progress, by_hash) -> None:
    """Count one row's status and report it; then its by-hash link."""
    rel = f"{base}/{repo_path}"
    is_pkg = kind == KIND_PKG
    if status == "linked":
        result.linked += 1
        result.linked_bytes += size
        result.linked_meta += not is_pkg
        if progress:
            progress(rel, "linked" if is_pkg else "linked-meta")
    elif status == "skipped":
        result.skipped += 1
        result.skipped_bytes += size
        result.skipped_meta += not is_pkg
        if progress:
            progress(rel, "skipped" if is_pkg else "skipped-meta")
    else:  # "error"
        result.errors.extend(added if added else [f"{rel}: link error"])
        return

    # by-hash link for apt index files — the pool key IS the sha256,
    # so this costs one hardlink, no hashing. Status is ignored: a
    # failed by-hash link is not fatal to the file it mirrors.
    if by_hash and _wants_by_hash(repo_path, kind):
        parent = str(Path(repo_path).parent)
        bh_base = f"{base}/{parent}" if parent != "." else base
        _linker(sink, pooled=True)(Path(pool_path), f"{bh_base}/by-hash/SHA256/{checksum}",
                                   size, checksum, kind)


def _dedup_rows(rows) -> list[tuple]:
    """One row per repo_path — the two sinks must not be handed a conflict.

    `pool.mirror_files()` is a `SELECT DISTINCT` over the whole row, so two
    component mirrors of one suite that synced either side of an upstream
    republish legitimately yield **two** rows for the same shared path (e.g.
    `dists/noble-updates/InRelease`) with different checksums. Handed that
    unresolved, the sinks disagreed: `TreeSink` took the LAST (each
    `hardlink_or_copy` replaces the previous one) and `BundleSink` took the
    FIRST (`if rel in self._seen: return "skipped"`) — so a published tree and
    a bundle of the same pool could ship different bytes at the same path,
    which is precisely the divergence `test_bundle_matches_publish_exactly`
    exists to make impossible.

    Neither checksum is more correct — both were real upstream content at
    some point in the same sync window, and the pool has no ordering that
    says which. What matters is that the answer is the *same* on both sides
    and stable across runs, so the tie is broken on the checksum itself
    (lexicographically greatest), which depends on nothing about SQLite's
    row order. Insertion order — and therefore the repo_path ordering
    `mirror_files()` produced for inode-cache locality — is preserved,
    because replacing a dict value keeps the key's original position.
    """
    chosen: dict[str, tuple] = {}
    for row in rows:
        repo_path, checksum = row[0], row[4]
        current = chosen.get(repo_path)
        if current is None or checksum > current[4]:
            chosen[repo_path] = row
    return list(chosen.values())


def publish_by_host(ctx, rows) -> tuple[list, set[str]]:
    """Payloads at their upstream `host/path`, one group per host (each host
    is a base) — for providers that serve at the real hostnames. `rows` are
    de-duplicated `mirror_files` rows; release files go last in each host."""
    from mmw.core.pool import KIND_RELEASE
    from mmw.core.publisher.results import MirrorPublishResult
    by_host: dict[str, list] = {}
    for repo_path, pool_path, size, kind, checksum in rows:
        host, rest = repo_path.split("/", 1)
        by_host.setdefault(host, []).append((rest, pool_path, size, kind, checksum))
    results = []
    for idx, host in enumerate(sorted(by_host)):
        if ctx.stop_event and ctx.stop_event.is_set():
            break
        host_rows = by_host[host]
        if ctx.mirror_start_callback:
            # A host is no unit the pre-publish counts know by name, so
            # its own count goes with it — package files only, as the bar
            # counts them (a `.metadata` beside each wheel is linked-meta).
            ctx.mirror_start_callback(host, idx + 1, len(by_host),
                                      count=sum(r[3] == KIND_PKG for r in host_rows))
        result = MirrorPublishResult(mirror_name=host)
        ordered = [r for r in host_rows if r[3] != KIND_RELEASE] + \
                  [r for r in host_rows if r[3] == KIND_RELEASE]
        if not _link_rows(ordered, host, ctx.sink, result, ctx.progress, ctx.stop_event,
                          by_hash=False):
            results.append(result)
            break
        if ctx.prune:
            keep = {f"{host}/{r[0]}" for r in host_rows}
            result.pruned, result.pruned_bytes = ctx.sink.finalize_group(
                host, keep, set(), set())
        results.append(result)
    return results, set(by_host)


def reconcile_extra_prefix(sink, prefix: str, keep: set[str]) -> tuple[int, int]:
    """Reconcile one non-mirror_base prefix (`nginx/`, `keys/`) on a sink.

    `finalize_group`'s contract is already exactly "make the destination hold
    exactly this `keep` set under this prefix", so these need no new sink
    method — only a caller that remembers they exist. `live_checksums` is
    empty because neither prefix ever contains by-hash paths.

    Shared by `publish_network` and `bundle_network`: the two pipelines differ
    only in their sink, and a second copy of this rule is exactly how the two
    halves of the publish/bundle pair drifted apart every other time.
    """
    return sink.finalize_group(prefix, keep, set(), set())


def publish_group(
    mirrors: list[MirrorConfig],
    sink,
    pool: Pool,
    progress=None,
    stop_event=None,
    prune: bool = True,
    prune_exclude: set[str] | None = None,
    keep_extra: set[str] | frozenset[str] = frozenset(),
) -> MirrorPublishResult:
    """Publish all mirrors sharing one mirror_base through a Sink.

    `keep_extra` names destination paths published by another step (upstream
    files — see `upstream_files`) that this group's prune must not remove
    even though they sit under its base and are not in its index.

    Order: packages → index files (+by-hash) → release files → finalize. A
    live-served destination is never left with a release file whose referenced
    files are missing, and a tar stream extracts in the same order.
    """
    lead = mirrors[0]
    base = lead.mirror_base
    names = [m.name for m in mirrors]
    result = MirrorPublishResult(mirror_name=lead.name)

    # One refs⋈packages query for all three kinds — on a large mirror set the
    # sort dominates, and running it once instead of per-kind keeps the silent
    # pre-link gap short. Rows stay repo_path-ordered within each bucket.
    if progress:
        progress(base, "enumerate-start")
    buckets: dict[str, list] = {KIND_PKG: [], KIND_META: [], KIND_RELEASE: []}
    for row in _dedup_rows(pool.mirror_files(names)):
        if row[0].startswith(ROOT_RELATIVE_PREFIX):
            continue
        bucket = buckets.get(row[3])
        if bucket is not None:
            bucket.append(row)
    pkg_rows, meta_rows, rel_rows = buckets[KIND_PKG], buckets[KIND_META], buckets[KIND_RELEASE]
    if progress:
        progress(base, f"enumerate-done:{len(pkg_rows) + len(meta_rows) + len(rel_rows)}")

    if not (pkg_rows or meta_rows or rel_rows):
        result.errors.append("not synced yet — no pool refs for this mirror")
        return result

    from mmw.providers import get_provider
    hashed = any(get_provider(m.provider).by_hash for m in mirrors)
    for rows, by_hash in ((pkg_rows, False), (meta_rows, hashed), (rel_rows, hashed)):
        if not _link_rows(rows, base, sink, result, progress, stop_event, by_hash):
            return result

    # ── Finalize: reconcile the destination to exactly what refs want ──────
    if prune and not (stop_event and stop_event.is_set()):
        keep = {f"{base}/{r[0]}" for r in (pkg_rows + meta_rows + rel_rows)}
        keep |= {r for r in keep_extra if r.startswith(base + "/")}
        live = {r[4] for r in (meta_rows + rel_rows)}
        # `finalize_group` returns this group's own (files, bytes) pruned —
        # `sink` may be shared across every group in a network publish, so its
        # own running totals are not this group's answer.
        result.pruned, result.pruned_bytes = sink.finalize_group(
            base, keep, live, prune_exclude or set())

    return result


def file_checksum(path: Path) -> str:
    """sha256 of a file on disk, streamed.

    Keys and the changelogs meta-release blobs are published from real files
    rather than from a pool row, so nothing hands their checksum in. They
    used to be linked with `checksum=""`, which made them invisible to
    `apply.py --verify`: `verify_tree` skips an empty-checksum entry, so a
    missing or corrupted **key** — the one artefact that makes every package
    beside it verifiable — was exactly what the far side's integrity check
    could not detect.
    """
    return disk_io.hash_file(path)


def _key_groups(mirrors: list[MirrorConfig]) -> set[str]:
    """The `keys/` directories a set of mirrors owns: each one's label, plus
    the repository's name as a fallback (see `publish_keys`) — the same
    fallback the sync writes under."""
    from mmw.core import upstream
    groups: set[str] = set()
    for mirror in mirrors:
        if mirror.label:
            groups.add(mirror.label)
        groups.add(upstream.repository_name(mirror))
    return groups


def upstream_files(mirror_dir: Path, mirrors: list[MirrorConfig]) -> dict[str, Path]:
    """`{host}/{path}` → the stored copy, for every file these mirrors'
    repositories fetched from a URL — a key URL or an `extra_files` entry
    (`upstream/{repository}/`, written during sync by `core/upstream.py`).

    Computed BEFORE any group is published, because a file can sit inside a
    repository's own directory (NVIDIA's `…/x86_64/3bf863cc.pub`) without
    being in its index — that group's prune has to be told to keep it, or a
    TreeSink deletes it every publish and a BundleSink tells the far side to.
    """
    from mmw.core import upstream
    found: dict[str, Path] = {}
    groups: set[str] = set()
    for m in mirrors:
        group = upstream.repository_group(m)
        if not disk_io.ops.is_dir(upstream.group_dir(mirror_dir, group)):
            # Not synced since the directory name became unique: serve what
            # the old one holds rather than drop the files from the tree.
            group = upstream.legacy_group(m) or group
        groups.add(group)
    for group in sorted(groups):
        base = upstream.group_dir(mirror_dir, group)
        if not disk_io.ops.is_dir(base):
            continue
        for f in sorted(disk_io.ops.rglob(base, "*")):
            if disk_io.ops.is_file(f) and not disk_io.ops.is_symlink(f) and not f.name.endswith(".tmp"):
                found.setdefault(f.relative_to(base).as_posix(), f)
    return found


def upstream_file_hosts(files: dict[str, Path]) -> list[str]:
    """Top-level directories (hosts) the upstream files live under — each
    needs a server block, and a name on the certificate."""
    return sorted({rel.split("/", 1)[0] for rel in files})


def upstream_file_live_dirs(files: dict[str, Path], bases: set[str]) -> set[str]:
    """Directories `prune_stale_bases` must treat as live so it does not
    remove upstream files right after they are published.

    A file's own directory is added unless it is already safe: inside a live
    base, or itself a parent of one (the sweep descends parents and only ever
    removes directories, never a file sitting in one). Everything else needs
    it — a host no repository uses (`keys.example.com/…`), and a directory
    beside a repository on its own host (Microsoft's
    `packages.microsoft.com/keys/microsoft.asc`, next to `repos/code`), which
    the sweep otherwise reads as a dropped repository. Only that exact
    directory is added: marking a *parent* of a base live would shield real
    stale repositories beside it.
    """
    from mmw.core.publisher.sinks import base_ancestors
    ancestors = base_ancestors(bases)
    live: set[str] = set()
    for rel in files:
        d = posixpath.dirname(rel)
        if not d or d in bases or d in ancestors:
            continue
        if any(d.startswith(b + "/") for b in bases):
            continue
        live.add(d)
    return live


def publish_upstream_files(sink, files: dict[str, Path]) -> list[str]:
    """Link each upstream copy to its own `{host}/{path}`. Errors are
    returned, exactly as loud as `publish_keys`'s."""
    errors: list[str] = []
    for rel, src in files.items():
        before = len(sink.errors)
        try:
            checksum = file_checksum(src)
        except OSError as e:
            errors.append(f"{rel}: {e}")
            continue
        if sink.link(src, rel, disk_io.ops.stat(src).st_size, checksum, KIND_META) == "error":
            added = sink.errors[before:]
            errors.extend(added if added else [f"{rel}: link error"])
    return errors


def publish_keys(sink, mirror_dir: Path,
                 mirrors: list[MirrorConfig]) -> tuple[list[str], set[str]]:
    """Publish GPG key directories to `keys/{label}/` on the sink.

    Returns `(errors, published_rels)`. The path set is what lets the caller
    reconcile `keys/` the way a mirror group is reconciled — a key file for a
    repository that has since been dropped from the network otherwise sits in
    the destination forever, because `keys/` is part of no `mirror_base` and
    therefore no `finalize_group` ever looks at it.

    Keyed on the **label** (and the derived source name as a fallback), because
    that is how `keys/` is grouped and — critically — what the pool actually
    stores.

    This used to have a second branch, per mirror *name*, gated on
    `mirror.gpg_key or mirror.gpg_key_url`. Publish reads its mirrors back from
    the pool, and the pool's `mirrors` table has **no key columns**, so those
    fields were always None and the branch never fired: every inline
    `Signed-By:` key — all of the NVIDIA ones on a DGX bundle — was written into
    `keys/` during sync and then silently never published. An airgapped machine
    got the packages and no way to verify them.

    The presence of a key directory is the fact; a config field the pool does
    not carry is not.

    Returns formatted errors for any key that failed to link — this function
    has no `MirrorPublishResult` of its own to append to (it runs once per
    network, not once per mirror group), so the caller folds these into one.
    A link failure here must be exactly as loud as one on a package: an
    unverifiable key silently missing from an airgapped bundle is the whole
    bug this function's docstring above already describes once.
    """
    keys_src = mirror_dir / "keys"
    if not disk_io.ops.is_dir(keys_src):
        return [], set()

    groups = _key_groups(mirrors)
    errors: list[str] = []
    published: set[str] = set()
    for group in sorted(groups):
        src_dir = keys_src / group
        if not disk_io.ops.is_dir(src_dir):
            continue
        for f in sorted(disk_io.ops.iterdir(src_dir)):
            if not disk_io.ops.is_file(f):
                continue
            rel = f"keys/{group}/{f.name}"
            before = len(sink.errors)
            try:
                checksum = file_checksum(f)
            except OSError as e:
                errors.append(f"{rel}: {e}")
                continue
            status = sink.link(f, rel, disk_io.ops.stat(f).st_size, checksum, KIND_META)
            if status == "error":
                added = sink.errors[before:]
                errors.extend(added if added else [f"{rel}: link error"])
            else:
                published.add(rel)
    return errors, published


__all__ = [
    "ROOT_RELATIVE_PREFIX",
    "atomic_write",
    "same_file",
    "file_checksum",
    "hardlink_or_copy",
    "is_package_file",
    "publish_group",
    "publish_keys",
    "publish_upstream_files",
    "upstream_file_hosts",
    "upstream_file_live_dirs",
    "upstream_files",
]
