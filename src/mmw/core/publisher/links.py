"""Hardlink publishing: materialize a network tree from the pool DB.

The destination is built in trust order — packages, then index files (with
their by-hash links), then release files LAST, then stale-file pruning — so a
client hitting a live-served mirror mid-publish always sees a release file
whose referenced files exist.

Writing itself goes through a `Sink` (`sinks.py`): this module owns
enumeration, ordering and accounting, not filesystem writes.
"""
import hashlib
import os
import posixpath
import shutil
from pathlib import Path

from mmw.core.config import MirrorConfig, derive_name
from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool

from mmw.core.publisher.results import MirrorPublishResult
from mmw.core.publisher.sinks import _prune_by_hash_dir  # noqa: F401 (re-export)

# Hardlinks are created in chunks so progress flows and stop is honored even
# when the phase is seek-bound on a cold inode cache.
_LINK_CHUNK = 2000


# refs whose repo_path starts with this are network-root-relative
# (changelogs.ubuntu.com meta-release files), not part of the mirror_base tree.
ROOT_RELATIVE_PREFIX = "changelogs.ubuntu.com/"


def stage_path(tmp_dir: Path, dst: Path) -> Path:
    """A collision-free staging name for `dst` inside `tmp_dir`.

    Hashed rather than derived by substitution, for the same reason
    `resources/apply.py:_tmp_path` hashes: a repo path can be arbitrarily
    long and deep, and flattening it into one directory by replacing "/"
    risks blowing a filesystem's max filename length.
    """
    digest = hashlib.sha256(str(dst).encode("utf-8", "surrogateescape")).hexdigest()
    return tmp_dir / digest


def atomic_write(dst: Path, data: bytes, tmp_dir: Path | None = None) -> None:
    """Write `data` at `dst` so a reader never sees a partial file.

    Same discipline as `hardlink_or_copy` below and for the same reason — a
    published tree is live-served with `autoindex on`.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if tmp_dir is None:
        tmp_dir = dst.parent
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = stage_path(tmp_dir, dst)
    try:
        tmp.write_bytes(data)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def hardlink_or_copy(src: Path, dst: Path, tmp_dir: Path | None = None) -> None:
    """Hardlink src → dst, falling back to copy on cross-device — atomically.

    The whole pkg → meta → release ordering this module is built around
    exists so a client mid-`apt update` never sees an inconsistent tree. That
    guarantee was undercut one file at a time: replacing a CHANGED file used
    to be `dst.unlink()` then `dst.hardlink_to(src)`, so every such file had a
    window in which it 404'd, and the `shutil.copy2` fallback wrote in place,
    so on a cross-device destination it had a window in which it was
    *truncated and servable* — a client could fetch half a Packages.gz and
    cache it. Ordering the file classes is pointless if each individual file
    is non-atomic.

    So the new content is built at a staging path and `os.replace`d onto the
    destination: `dst` only ever transitions old-content → new-content, never
    through absent or partial. `tmp_dir` should be a directory on the
    destination's own filesystem that is never served — `TreeSink` passes
    `{root}/.mmw/tmp/`, the same place `apply.py` stages on the far side, and
    for the same reason (`{dst}.mmw-tmp` beside the real file was listed and
    fetchable under `autoindex on`).
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        try:
            if dst.stat().st_ino == src.stat().st_ino:
                return
        except OSError:
            pass
    if tmp_dir is None:
        tmp_dir = dst.parent
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = stage_path(tmp_dir, dst)
    try:
        if tmp.exists():
            tmp.unlink()
        try:
            tmp.hardlink_to(src)
        except OSError:
            shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


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
        for repo_path, pool_path, size, kind, checksum in rows[i:i + _LINK_CHUNK]:
            rel = f"{base}/{repo_path}"
            is_pkg = kind == KIND_PKG

            # `errors` is part of the Sink protocol: capture the length before
            # the call so an "error" status can attribute exactly the entries
            # THIS call added, not just assume the last one is ours.
            before = len(sink.errors)
            status = sink.link(Path(pool_path), rel, size, checksum, kind)
            if status == "linked":
                result.linked += 1
                result.linked_bytes += size
                if progress:
                    progress(rel, "linked" if is_pkg else "linked-meta")
            elif status == "skipped":
                result.skipped += 1
                result.skipped_bytes += size
                if progress:
                    progress(rel, "skipped" if is_pkg else "skipped-meta")
            else:  # "error"
                added = sink.errors[before:]
                result.errors.extend(added if added else [f"{rel}: link error"])
                continue

            # by-hash link for apt index files — the pool key IS the sha256,
            # so this costs one hardlink, no hashing. Status is ignored: a
            # failed by-hash link is not fatal to the file it mirrors.
            if by_hash and _wants_by_hash(repo_path, kind):
                parent = str(Path(repo_path).parent)
                bh_base = f"{base}/{parent}" if parent != "." else base
                sink.link(Path(pool_path), f"{bh_base}/by-hash/SHA256/{checksum}",
                         size, checksum, kind)
    return True


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

    apt_like = any(m.provider in ("apt", "apt-flat") for m in mirrors)
    for rows, by_hash in ((pkg_rows, False), (meta_rows, apt_like), (rel_rows, apt_like)):
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
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _key_groups(mirrors: list[MirrorConfig]) -> set[str]:
    """The `keys/` directories a set of mirrors owns: each one's label, plus
    the derived source name as a fallback (see `publish_keys`)."""
    groups: set[str] = set()
    for mirror in mirrors:
        if mirror.label:
            groups.add(mirror.label)
        source = getattr(mirror, "source", "") or mirror.source_url
        if source:
            groups.add(derive_name(source))
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
    for group in sorted({upstream.repository_group(m) for m in mirrors}):
        base = upstream.group_dir(mirror_dir, group)
        if not base.is_dir():
            continue
        for f in sorted(base.rglob("*")):
            if f.is_file() and not f.is_symlink() and not f.name.endswith(".tmp"):
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
        if sink.link(src, rel, src.stat().st_size, checksum, KIND_META) == "error":
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
    if not keys_src.is_dir():
        return [], set()

    groups = _key_groups(mirrors)
    errors: list[str] = []
    published: set[str] = set()
    for group in sorted(groups):
        src_dir = keys_src / group
        if not src_dir.is_dir():
            continue
        for f in sorted(src_dir.iterdir()):
            if not f.is_file():
                continue
            rel = f"keys/{group}/{f.name}"
            before = len(sink.errors)
            try:
                checksum = file_checksum(f)
            except OSError as e:
                errors.append(f"{rel}: {e}")
                continue
            status = sink.link(f, rel, f.stat().st_size, checksum, KIND_META)
            if status == "error":
                added = sink.errors[before:]
                errors.extend(added if added else [f"{rel}: link error"])
            else:
                published.add(rel)
    return errors, published



__all__ = [
    "ROOT_RELATIVE_PREFIX",
    "atomic_write",
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
