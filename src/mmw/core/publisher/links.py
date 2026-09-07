"""Hardlink publishing: materialize a network tree from the pool DB.

The destination is built in trust order — packages, then index files (with
their by-hash links), then release files LAST, then stale-file pruning — so a
client hitting a live-served mirror mid-publish always sees a release file
whose referenced files exist.

Writing itself goes through a `Sink` (`sinks.py`): this module owns
enumeration, ordering and accounting, not filesystem writes.
"""
import shutil
from pathlib import Path

from mmw.core.config import MirrorConfig, derive_name
from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool

from mmw.core.publisher.results import MirrorPublishResult
from mmw.core.publisher.sinks import _prune_by_hash_dir  # noqa: F401 (re-export)

# Hardlinks are created in chunks so progress flows and stop is honored even
# when the phase is seek-bound on a cold inode cache.
_LINK_CHUNK = 2000

_APT_INDEX_PREFIXES = ("Packages", "Sources", "Release", "InRelease", "Translation-")

# refs whose repo_path starts with this are network-root-relative
# (changelogs.ubuntu.com meta-release files), not part of the mirror_base tree.
ROOT_RELATIVE_PREFIX = "changelogs.ubuntu.com/"


def hardlink_or_copy(src: Path, dst: Path) -> None:
    """Hardlink src → dst, falling back to copy on cross-device."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        try:
            if dst.stat().st_ino == src.stat().st_ino:
                return
        except OSError:
            pass
        dst.unlink()
    try:
        dst.hardlink_to(src)
    except OSError:
        shutil.copy2(src, dst)


def is_package_file(rel: Path) -> bool:
    """True for package payload files that are tracked in the pool."""
    parts = rel.parts
    # APT: pool/<component>/<letter>/<package>/<file>
    if parts and parts[0] == "pool":
        return True
    return False


def _wants_by_hash(repo_path: str) -> bool:
    name = Path(repo_path).name
    return any(name.startswith(p) for p in _APT_INDEX_PREFIXES)


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
            if by_hash and _wants_by_hash(repo_path):
                parent = str(Path(repo_path).parent)
                bh_base = f"{base}/{parent}" if parent != "." else base
                sink.link(Path(pool_path), f"{bh_base}/by-hash/SHA256/{checksum}",
                         size, checksum, kind)
    return True


def publish_group(
    mirrors: list[MirrorConfig],
    sink,
    pool: Pool,
    progress=None,
    stop_event=None,
    prune: bool = True,
    prune_exclude: set[str] | None = None,
) -> MirrorPublishResult:
    """Publish all mirrors sharing one mirror_base through a Sink.

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
    for row in pool.mirror_files(names):
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
        live = {r[4] for r in (meta_rows + rel_rows)}
        # `finalize_group` returns this group's own (files, bytes) pruned —
        # `sink` may be shared across every group in a network publish, so its
        # own running totals are not this group's answer.
        result.pruned, result.pruned_bytes = sink.finalize_group(
            base, keep, live, prune_exclude or set())

    return result


def publish_keys(sink, mirror_dir: Path, mirrors: list[MirrorConfig]) -> None:
    """Publish GPG key directories to `keys/{label}/` on the sink.

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
    """
    keys_src = mirror_dir / "keys"
    if not keys_src.is_dir():
        return

    groups: set[str] = set()
    for mirror in mirrors:
        if mirror.label:
            groups.add(mirror.label)
        source = getattr(mirror, "source", "") or mirror.source_url
        if source:
            groups.add(derive_name(source))

    for group in sorted(groups):
        src_dir = keys_src / group
        if not src_dir.is_dir():
            continue
        for f in sorted(src_dir.iterdir()):
            if f.is_file():
                sink.link(f, f"keys/{group}/{f.name}", f.stat().st_size, "", KIND_META)



__all__ = [
    "ROOT_RELATIVE_PREFIX",
    "hardlink_or_copy",
    "is_package_file",
    "publish_group",
    "publish_keys",
]
