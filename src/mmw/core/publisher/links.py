"""Hardlink publishing: materialize a network tree from the pool DB.

The destination is built in trust order — packages, then index files (with
their by-hash links), then release files LAST, then stale-file pruning — so a
client hitting a live-served mirror mid-publish always sees a release file
whose referenced files exist.
"""
import os
import shutil
from pathlib import Path

from mmw.core.config import MirrorConfig, NetworkConfig, derive_name
from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool

from mmw.core.publisher.results import MirrorPublishResult
from mmw.core.publisher.stats import publish_base

# Hardlinks are created in chunks so progress flows and stop is honored even
# when the phase is seek-bound on a cold inode cache.
_LINK_CHUNK = 2000

_APT_INDEX_PREFIXES = ("Packages", "Sources", "Release", "InRelease", "Translation-")

# The destination scan reports progress every this many files, so a long walk
# over a live tree can never look like a hang.
_SCAN_REPORT_EVERY = 5000

# Stale by-hash generations to retain beyond the current one — apt clients mid
# `apt update` may still request hashes from the previous Release.
_BY_HASH_KEEP_STALE = 2

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
    dst_root: Path,
    result: MirrorPublishResult,
    progress,
    stop_event,
    by_hash: bool,
) -> bool:
    """Hardlink (repo_path, pool_path, …) rows under dst_root, chunked.

    Returns False if interrupted by the stop event.
    """
    for i in range(0, len(rows), _LINK_CHUNK):
        if stop_event and stop_event.is_set():
            return False
        for repo_path, pool_path, size, kind, checksum in rows[i:i + _LINK_CHUNK]:
            src = Path(pool_path)
            dst = dst_root / repo_path
            is_pkg = kind == KIND_PKG

            try:
                if dst.exists() and dst.stat().st_ino == src.stat().st_ino:
                    result.skipped += 1
                    result.skipped_bytes += size
                    if progress:
                        progress(dst, "skipped" if is_pkg else "skipped-meta")
                else:
                    try:
                        hardlink_or_copy(src, dst)
                        result.linked += 1
                        result.linked_bytes += size
                        if progress:
                            progress(dst, "linked" if is_pkg else "linked-meta")
                    except OSError as e:
                        result.errors.append(f"{repo_path}: {e}")
                        continue
            except OSError as e:
                result.errors.append(f"{repo_path}: {e}")
                continue

            # by-hash link for apt index files — the pool key IS the sha256,
            # so this costs one hardlink, no hashing.
            if by_hash and _wants_by_hash(repo_path):
                bh_link = dst.parent / "by-hash" / "SHA256" / checksum
                try:
                    hardlink_or_copy(src, bh_link)
                except OSError:
                    pass
    return True


def _prune_by_hash_dir(bh_dir: Path, live_checksums: set[str]) -> int:
    """Retire stale by-hash generations, keeping the newest few for in-flight
    clients. An entry's name IS its checksum, so staleness is exact: not in
    the current refs set. (nlink can't be used here — the pool keeps old
    blobs' link counts above 1.)"""
    stale = []
    try:
        for e in bh_dir.iterdir():
            try:
                if e.is_file() and e.name not in live_checksums:
                    stale.append((e.stat().st_mtime, e))
            except OSError:
                pass
    except OSError:
        return 0
    stale.sort(reverse=True)
    pruned = 0
    for _, e in stale[_BY_HASH_KEEP_STALE:]:
        try:
            e.unlink()
            pruned += 1
        except OSError:
            pass
    return pruned


def publish_group(
    mirrors: list[MirrorConfig],
    network: NetworkConfig,
    pool: Pool,
    progress=None,
    stop_event=None,
    prune: bool = True,
    prune_exclude: set[Path] | None = None,
) -> MirrorPublishResult:
    """Publish all mirrors sharing one mirror_base from the pool DB.

    Order: packages → index files (+by-hash) → release files → prune. A
    live-served destination is never left with a release file whose referenced
    files are missing.
    """
    lead = mirrors[0]
    names = [m.name for m in mirrors]
    result = MirrorPublishResult(mirror_name=lead.name)
    dst_root = publish_base(network, lead)

    # One refs⋈packages query for all three kinds — on a large mirror set the
    # sort dominates, and running it once instead of per-kind keeps the silent
    # pre-link gap short. Rows stay repo_path-ordered within each bucket.
    if progress:
        progress(dst_root, "enumerate-start")
    buckets: dict[str, list] = {KIND_PKG: [], KIND_META: [], KIND_RELEASE: []}
    for row in pool.mirror_files(names):
        if row[0].startswith(ROOT_RELATIVE_PREFIX):
            continue
        bucket = buckets.get(row[3])
        if bucket is not None:
            bucket.append(row)
    pkg_rows, meta_rows, rel_rows = buckets[KIND_PKG], buckets[KIND_META], buckets[KIND_RELEASE]
    if progress:
        progress(dst_root, f"enumerate-done:{len(pkg_rows) + len(meta_rows) + len(rel_rows)}")

    if not (pkg_rows or meta_rows or rel_rows):
        result.errors.append("not synced yet — no pool refs for this mirror")
        return result

    apt_like = any(m.provider in ("apt", "apt-flat") for m in mirrors)

    if not _link_rows(pkg_rows, dst_root, result, progress, stop_event, by_hash=False):
        return result
    if not _link_rows(meta_rows, dst_root, result, progress, stop_event, by_hash=apt_like):
        return result
    # Trust roots last: only after everything they reference exists.
    if not _link_rows(rel_rows, dst_root, result, progress, stop_event, by_hash=apt_like):
        return result

    # ── Prune: anything at the destination the refs don't want ────────────────
    if prune and dst_root.exists() and not (stop_event and stop_event.is_set()):
        keep = {r[0] for r in (pkg_rows + meta_rows + rel_rows)}
        live_checksums = {r[4] for r in (meta_rows + rel_rows)}
        bh_pruned = 0
        bh_dirs: set[Path] = set()
        checked = 0
        if progress:
            progress(dst_root, "prune-scan-start")
        # os.walk (scandir) rather than rglob: kept files — the overwhelming
        # majority — cost no stat at all, and whole subtrees are skipped by
        # pruning dirnames instead of testing every file under them.
        for dirpath, dirnames, filenames in os.walk(dst_root):
            if stop_event and stop_event.is_set():
                break
            here = Path(dirpath)
            if "by-hash" in here.parts:
                # by-hash entries are retired by generation, not against keep.
                if filenames:
                    bh_dirs.add(here)
                continue
            if prune_exclude and any(here.is_relative_to(x) for x in prune_exclude):
                dirnames[:] = []
                continue
            rel_dir = here.relative_to(dst_root)
            for fname in filenames:
                checked += 1
                if progress and checked % _SCAN_REPORT_EVERY == 0:
                    progress(here, f"prune-scan-progress:{checked}")
                if str(rel_dir / fname) in keep:
                    continue
                dst = here / fname
                try:
                    size = dst.stat().st_size
                    dst.unlink()
                    result.pruned += 1
                    result.pruned_bytes += size
                    if progress:
                        progress(dst, "pruned")
                except OSError:
                    pass
        for bh_dir in bh_dirs:
            bh_pruned += _prune_by_hash_dir(bh_dir, live_checksums)
        if progress:
            if bh_pruned:
                progress(dst_root, f"by-hash:{bh_pruned}")
            progress(dst_root, f"prune-scan-done:{checked}:{result.pruned}")

    return result


def publish_keys(network: NetworkConfig, mirror_dir: Path, mirrors: list[MirrorConfig]) -> None:
    """Publish GPG key directories into {network.root}/keys/.

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
    keys_dst = network.root / "keys"
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
                hardlink_or_copy(f, keys_dst / group / f.name)



__all__ = [
    "ROOT_RELATIVE_PREFIX",
    "hardlink_or_copy",
    "is_package_file",
    "publish_group",
    "publish_keys",
]
