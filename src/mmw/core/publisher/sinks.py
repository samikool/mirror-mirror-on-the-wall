"""Destination sinks: one publish pipeline, two kinds of destination.

`publish_group` enumerates from the pool and orders pkg → meta → release; a
Sink decides what "put this file there" means. `TreeSink` hardlinks into a
directory (a live-served mirror); `BundleSink` streams into a tar.zst that
crosses an airgap.

`rel` is always network-root-relative and POSIX:
    archive.ubuntu.com/ubuntu/dists/noble/Release

**The `progress` callback a Sink is constructed with follows the same rule:**
every call is `progress(rel: str, status: str)` — a sink-relative POSIX
string, never a `Path`. `BundleSink` has no destination path to hand back at
all, so a contract that assumed one could not survive it. The one production
consumer, `PublishProgressModal.record` (`tui/publish_modals.py`), expects a
`str` and will raise on a `Path`.
"""
import contextlib
import hashlib
import io
import os
import shutil
import tarfile
import time
from dataclasses import replace
from pathlib import Path
from typing import Protocol

import zstandard

from mmw.core.publisher.manifest import ManifestHeader, write_manifest
from mmw.core.publisher.nginx import ROOT_PLACEHOLDER

_SCAN_REPORT_EVERY = 5000
_BY_HASH_KEEP_STALE = 2
_ARCHIVE_REPORT_INTERVAL = 0.25  # seconds — matches the TUI's own EMA sample rate
# A file this size or larger gets its own immediate, unthrottled "now
# compressing X" announcement the instant it starts — not just whenever it
# finishes. The throttled per-file sampling below only ever reports a file
# AFTER tar.addfile() returns, so one big file (this pool holds driver/kernel
# packages in the multiple-GB range) can leave the log silent for however
# long that single file takes, reading as a stall rather than real,
# in-progress work. 50 MB is comfortably above where most content sits (this
# pool: 43% of blobs under 50 KB) and comfortably below where a single file's
# own processing time could plausibly outlast the report interval.
_ANNOUNCE_LARGE_FILE_BYTES = 50 * 1024 * 1024

# Root-relative things a network destination holds that belong to no
# `mirror_base` at all, and which `prune_stale_bases` must therefore never
# treat as the leftovers of a dropped repository. `nginx/` and `keys/` get
# their own reconcile pass instead (`finalize_group` with that prefix, driven
# by `publish_network`/`bundle_network`); `changelogs.ubuntu.com/` belongs to
# the dist-upgrade step; `.mmw/` is apply.py's staging area and `.mmw-state`
# its ledger cursor — deleting either from a far side would be catastrophic.
NON_BASE_DIRS = frozenset({"nginx", "keys", "changelogs.ubuntu.com", ".mmw"})

# Where a `bundle_tls` network's certificate and key land on the far side:
# under `.mmw/`, which no generated server block serves (each one's root is a
# hostname directory). Fixed control members like `.mmw/apply.py`, not
# manifest payload — so they ride every bundle, delta or full, and sit near
# the front of the archive rather than in the tail a cut-short copy loses.
TLS_DIR = ".mmw/tls"
NON_BASE_FILES = frozenset({"UPGRADE.md", ".mmw-state", "apply.py"})

# Where TreeSink stages content before renaming it onto a live-served path.
# Never inside a `mirror_base`, so nginx can never serve it — the same
# reasoning as apply.py's own `{root}/.mmw/tmp/`.
_TMP_SUBDIR = (".mmw", "tmp")


def _is_stale_base(rel_dir: str, live_bases: set[str], ancestors: set[str]) -> bool:
    """True when `rel_dir` is a directory no live mirror_base needs.

    Shared by both sinks so "which subtree belongs to a repository this
    network no longer selects" has exactly one definition.
    """
    if rel_dir in live_bases or rel_dir in ancestors:
        return False
    return not any(rel_dir.startswith(b + "/") for b in live_bases)


def base_ancestors(live_bases: set[str]) -> set[str]:
    """Every partial path on the way down to a live base.

    A `mirror_base` is multi-segment (`archive.ubuntu.com/ubuntu`), so the
    host directory above it must be descended into rather than deleted.
    """
    out: set[str] = set()
    for base in live_bases:
        parts = base.split("/")
        for i in range(1, len(parts)):
            out.add("/".join(parts[:i]))
    return out


class _CountingWriter:
    """Wraps a binary file object, tallying bytes actually written to it.

    zstandard's `stream_writer` sits between tarfile and the real file
    handle, so this is the only point that sees genuine compressed-on-disk
    bytes — reading `tmp_out.stat().st_size` mid-write would race an
    unflushed compressor buffer. Everything but `write()` is forwarded
    straight to the wrapped handle (`flush()`, `tell()`, …) since zstandard
    and tarfile both expect a real file-like object.
    """

    def __init__(self, fh):
        self._fh = fh
        self.written = 0

    def write(self, data: bytes) -> int:
        n = self._fh.write(data)
        self.written += n if n is not None else len(data)
        return n

    def __getattr__(self, name):
        return getattr(self._fh, name)


class _ProgressFile:
    """Wraps a file object being read; reports percentage-through progress on
    a large file as `tarfile.addfile()`'s internal copy loop calls `.read()`.

    Only used for files at or above `_ANNOUNCE_LARGE_FILE_BYTES` — a small
    file finishes fast enough that the per-file sampling in
    `_maybe_report_archive_progress` already covers it; a multi-GB one can
    otherwise sit at "compressing…" with no sense of how far through it is.
    `tarfile` needs no changes: it only ever calls `.read()` in a loop, so
    wrapping the handle is enough — no internal offset/padding bookkeeping to
    replicate.
    """

    def __init__(self, fh, size: int, progress, rel: str):
        self._fh = fh
        self._size = size
        self._progress = progress
        self._rel = rel
        self._done = 0
        self._last_report = 0.0

    def read(self, n: int = -1) -> bytes:
        chunk = self._fh.read(n)
        self._done += len(chunk)
        now = time.monotonic()
        if now - self._last_report >= _ARCHIVE_REPORT_INTERVAL:
            self._last_report = now
            self._progress(self._rel, f"archive-large-file-progress:{self._done}:{self._size}")
        return chunk

    def __getattr__(self, name):
        return getattr(self._fh, name)


class Sink(Protocol):
    errors: list[str]
    """Every failure a `link()` call recorded, in call order. `_link_rows`
    slices this (`sink.errors[before:]`) to attribute the entries one `link()`
    call added to that call's own row — an explicit contract instead of an
    implicit "the last entry belongs to the caller" assumption."""

    def link(self, src: Path, rel: str, size: int, checksum: str, kind: str) -> str:
        """Place a pool blob at `rel`. Returns 'linked' | 'skipped' | 'error'.

        On 'error', append the failure detail to `self.errors` before
        returning — `_link_rows` reads it from there, not from the return
        value.
        """
        ...

    def write(self, rel: str, data: bytes) -> None:
        """Place generated bytes at `rel`."""
        ...

    def finalize_group(self, base: str, keep: set[str],
                       live_checksums: set[str], exclude: set[str]) -> tuple[int, int]:
        """Reconcile the destination under `base` to hold exactly `keep`.

        Returns (files_pruned, bytes_pruned) for THIS group only. A sink may be
        reused across every group in one network publish (`TreeSink` is, so
        by-hash retirement and destination pruning share one instance across a
        multi-repository network), so this cannot be a running total —
        `publish_group` adds the return value straight onto that group's own
        `MirrorPublishResult`. `BundleSink` returns
        (len(deletions recorded for this group), 0).
        """
        ...

    def prune_stale_bases(self, live_bases: set[str]) -> tuple[int, int]:
        """Remove whole `mirror_base` subtrees no group produced this run.

        `finalize_group` is scoped to one base, so a base that is no longer
        produced by ANY group is reached by nothing: narrowing
        `[[networks.select]]` left the entire `security.ubuntu.com/ubuntu/`
        subtree served forever, stale `InRelease` and all, and no delta could
        ever delete it either. Destructive, so callers only invoke it when
        `prune` is on. `NON_BASE_DIRS`/`NON_BASE_FILES` are never touched.
        """
        ...

    def close(self) -> None:
        ...


class TreeSink:
    """Hardlinks into a directory — the behaviour publish has always had."""

    def __init__(self, root: Path, progress=None, stop_event=None):
        self.root = root
        self._progress = progress
        self._stop_event = stop_event
        self.pruned = 0
        self.pruned_bytes = 0
        self.errors: list[str] = []

    @property
    def tmp_dir(self) -> Path:
        return self.root.joinpath(*_TMP_SUBDIR)

    def link(self, src: Path, rel: str, size: int, checksum: str, kind: str) -> str:
        from mmw.core.publisher.links import hardlink_or_copy
        dst = self.root / rel
        try:
            if dst.exists() and dst.stat().st_ino == src.stat().st_ino:
                return "skipped"
            hardlink_or_copy(src, dst, tmp_dir=self.tmp_dir)
            return "linked"
        except OSError as e:
            self.errors.append(f"{rel}: {e}")
            return "error"

    def write(self, rel: str, data: bytes) -> None:
        # A TreeSink destination is live-served. The nginx root placeholder
        # must always have already been substituted for a real path by the
        # caller (`str(network.root)`, publisher/__init__.py and bundle.py) —
        # an unsubstituted placeholder reaching disk here is a bug in that
        # caller, not something this sink should silently write out. See
        # nginx.py's own docstring: "An unsubstituted placeholder reaching
        # disk on either side is a bug."
        if ROOT_PLACEHOLDER.encode() in data:
            raise ValueError(
                f"{rel}: unsubstituted {ROOT_PLACEHOLDER} would have been "
                "written to a live-served destination"
            )
        from mmw.core.publisher.links import atomic_write
        # Staged-then-renamed like every other write into a live-served tree:
        # a client fetching nginx/mirror.conf or UPGRADE.md mid-publish gets
        # the old file or the new one, never a half-written one.
        atomic_write(self.root / rel, data, tmp_dir=self.tmp_dir)

    def finalize_group(self, base: str, keep: set[str],
                       live_checksums: set[str], exclude: set[str]) -> tuple[int, int]:
        dst_root = self.root / base if base else self.root
        if not dst_root.exists():
            return 0, 0
        group_pruned = 0
        group_pruned_bytes = 0
        bh_dirs: set[Path] = set()
        checked = 0
        # `progress` always receives a sink-relative POSIX str, never a Path —
        # a BundleSink has no destination path at all, so the callback
        # contract cannot assume one. `base` stands in for `dst_root` here.
        if self._progress:
            self._progress(base, "prune-scan-start")
        # os.walk (scandir) rather than rglob: kept files — the overwhelming
        # majority — cost no stat at all, and whole subtrees are skipped by
        # pruning dirnames instead of testing every file under them.
        seen_dirs: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(dst_root):
            if self._stop_event and self._stop_event.is_set():
                break
            here = Path(dirpath)
            seen_dirs.append(here)
            if "by-hash" in here.parts:
                if filenames:
                    bh_dirs.add(here)
                continue
            rel_dir = here.relative_to(self.root).as_posix()
            if any(rel_dir == x or rel_dir.startswith(x + "/") for x in exclude):
                dirnames[:] = []
                continue
            for fname in filenames:
                checked += 1
                if self._progress and checked % _SCAN_REPORT_EVERY == 0:
                    self._progress(rel_dir, f"prune-scan-progress:{checked}")
                rel = f"{rel_dir}/{fname}" if rel_dir != "." else fname
                if rel in keep:
                    continue
                target = here / fname
                try:
                    size = target.stat().st_size
                    target.unlink()
                    group_pruned += 1
                    group_pruned_bytes += size
                    if self._progress:
                        self._progress(rel, "pruned")
                except OSError:
                    pass
        bh_pruned = 0
        for bh_dir in bh_dirs:
            bh_pruned += _prune_by_hash_dir(bh_dir, live_checksums)
        _rmdir_empty(seen_dirs, keep_root=dst_root)
        # Cumulative totals across every group a shared sink processes — handy
        # for a caller tracking overall progress — are kept separately from
        # the per-group return value the result accounting relies on.
        self.pruned += group_pruned
        self.pruned_bytes += group_pruned_bytes
        if self._progress:
            if bh_pruned:
                self._progress(base, f"by-hash:{bh_pruned}")
            self._progress(base, f"prune-scan-done:{checked}:{group_pruned}")
        return group_pruned, group_pruned_bytes

    def prune_stale_bases(self, live_bases: set[str]) -> tuple[int, int]:
        """See the Sink protocol. Walks down from the root only as far as the
        live bases' own ancestors, so a whole dropped repository costs one
        `rmtree` and a healthy tree costs a handful of `scandir` calls."""
        if not self.root.is_dir():
            return 0, 0
        ancestors = base_ancestors(live_bases)
        removed = 0
        removed_bytes = 0
        stack = [""]
        while stack:
            rel_dir = stack.pop()
            here = self.root / rel_dir if rel_dir else self.root
            try:
                entries = list(here.iterdir())
            except OSError:
                continue
            for entry in entries:
                # A symlink is never something publish created, and following
                # one out of the tree with rmtree is exactly the mistake a
                # destructive sweep must not be capable of making.
                if entry.is_symlink() or not entry.is_dir():
                    continue
                rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                if not rel_dir and (entry.name in NON_BASE_DIRS
                                    or entry.name in NON_BASE_FILES):
                    continue
                if rel in ancestors:
                    stack.append(rel)
                    continue
                if not _is_stale_base(rel, live_bases, ancestors):
                    continue
                for f in entry.rglob("*"):
                    try:
                        if f.is_file() and not f.is_symlink():
                            removed_bytes += f.stat().st_size
                            removed += 1
                    except OSError:
                        pass
                shutil.rmtree(entry, ignore_errors=True)
                if self._progress:
                    self._progress(rel, "pruned-base")
        self.pruned += removed
        self.pruned_bytes += removed_bytes
        return removed, removed_bytes

    def close(self) -> None:
        # Debris from a killed publish: not servable (nothing under
        # `{root}/.mmw/` is inside any mirror_base, so no nginx site roots
        # reach it), but it would accumulate forever otherwise. One listdir.
        try:
            for entry in self.tmp_dir.iterdir():
                try:
                    entry.unlink()
                except OSError:
                    pass
        except OSError:
            pass


def _rmdir_empty(dirs: list[Path], keep_root: Path) -> int:
    """Remove now-empty directories, deepest first.

    The file sweep above deletes files and leaves their directories standing.
    The generated nginx sets `autoindex on`, so over a few suite rotations a
    client browsing the tree sees a skeleton of empty directories that
    nothing ever cleans up. `rmdir` only ever succeeds on a genuinely empty
    directory, so this can neither remove a kept file nor touch an excluded
    subtree (which still has files in it).
    """
    removed = 0
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        if d == keep_root:
            continue
        try:
            d.rmdir()
            removed += 1
        except OSError:
            pass
    return removed


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


class BundleSink:
    """Collects a bundle plan, then writes one tar.zst in `close()`.

    Not a streaming writer: `.mmw/manifest.jsonl.zst` must be FIRST in the tar
    stream so apply.py can validate before extracting anything, and the
    manifest is only complete once every group has been enumerated. So link()
    and write() record rows and do no tar I/O. Payload bytes are never
    buffered — only (rel, src, size, checksum) tuples, read straight from the
    pool blob when close() finally streams the archive.
    """

    def __init__(self, out_path: Path, header: ManifestHeader, prev: dict[str, str],
                apply_script: bytes, level: int = 3, prune: bool = True,
                full: bool = False, progress=None, compress: bool = True,
                tls_files: dict[str, bytes] | None = None):
        self.out_path = out_path
        # name → bytes, written as `{TLS_DIR}/{name}`; a name ending `.key`
        # goes in 0600. Empty unless the network opted in with bundle_tls.
        self._tls_files = dict(tls_files or {})
        self.header = header
        self._prev = prev
        self._apply_script = apply_script
        self._level = level
        self._prune = prune
        # Whether the tar payload itself is zstd-compressed. `.mmw/
        # manifest.jsonl.zst` is unaffected either way — it's a small,
        # independently-compressed blob apply.py's read_manifest_bytes()
        # already decompresses on its own, regardless of the outer container.
        self._compress = compress
        # link()/write() only ever append to in-memory lists — close() is
        # where the real I/O happens (reading pool blobs, streaming them
        # through zstd into the tar), so it is the only place archive
        # progress can be reported from. Same `progress(rel, status)`
        # contract as the Sink protocol, with its own status vocabulary
        # (`archive-start`/`archive-progress`/`archive-done`) — see close().
        self._progress = progress
        # `--full` means "send everything", never "forget what the far side
        # has". `prev` is populated the same way for a full bundle as for a
        # delta one (bundle.py always reads the latest manifest) — the delete
        # set in `finalize_group` is computed from it exactly as usual. `full`
        # only bypasses `prev` for the SEND decision in link()/write() below.
        self._full = full

        self.manifest_rows: list[tuple[str, str]] = []
        # The header actually written into the tar, complete with
        # `file_count`; set by close(). `bundle_network` writes THIS to the
        # ledger so the ledger is a faithful record of what shipped.
        self.written_header: ManifestHeader | None = None
        self._send: list[tuple[str, Path, int, str]] = []
        self._generated: dict[str, bytes] = {}
        self._seen: set[str] = set()
        self.deleted: list[str] = []
        self.sent = 0
        self.sent_bytes = 0
        self.errors: list[str] = []

    def link(self, src: Path, rel: str, size: int, checksum: str, kind: str) -> str:
        if rel in self._seen:
            return "skipped"
        self._seen.add(rel)
        self.manifest_rows.append((rel, checksum))
        # `and checksum` is a deliberate divergence from manifest.delta()'s
        # pure `prev.get(rel) != cs` set difference: an empty checksum (a
        # published GPG key — `publish_keys` calls `sink.link(f, rel,
        # size, "", KIND_META)`, see links.py) must never compare equal to
        # itself across bundles and be treated as "unchanged, skip resend" —
        # a key has no content hash to trust that judgement on, so it is
        # always resent. `test_bundle_sink_delta_agrees_with_manifest_delta`
        # only asserts the two rules agree where both have a real checksum.
        if not self._full and self._prev.get(rel) == checksum and checksum:
            return "skipped"
        self._send.append((rel, src, size, checksum))
        self.sent += 1
        self.sent_bytes += size
        return "linked"

    def write(self, rel: str, data: bytes) -> None:
        checksum = hashlib.sha256(data).hexdigest()
        self._seen.add(rel)
        self.manifest_rows.append((rel, checksum))
        if not self._full and self._prev.get(rel) == checksum:
            return
        self._generated[rel] = data
        self.sent += 1
        self.sent_bytes += len(data)

    def finalize_group(self, base: str, keep: set[str],
                       live_checksums: set[str], exclude: set[str]) -> tuple[int, int]:
        """A bundle cannot delete; it records what the far side must remove.

        Always returns a tuple — never `None` — even when this network isn't
        pruning: `publish_group` unpacks the return value unconditionally
        (`result.pruned, result.pruned_bytes = sink.finalize_group(...)`), so
        a bare `return` here would raise on every non-pruning network.

        `by-hash/` paths need their own rule, exactly as `TreeSink` gives them
        one (`if "by-hash" in here.parts: ...` before the generic sweep,
        `sinks.py`). `publish_group` builds `keep` from canonical repo_paths
        only — by-hash entries are synthesized separately by `_link_rows`
        (`links.py`) and are never members of `keep` by construction. Running
        the generic `rel not in keep` sweep over them would therefore schedule
        every *live* by-hash path for deletion: its checksum is unchanged, so
        `link()` correctly never re-sends it, and the naive sweep would then
        tell the far side to delete a file this same bundle's own manifest
        still lists as present — permanently eroding by-hash on every
        pruning apt network, one bundle at a time, since by-hash exists
        precisely to protect a client mid-`apt update`.

        **That fix was only ever applied to the flat case.** The `by-hash`
        branch below used to `continue` *before* the `exclude` test, so the
        outer group of a nested publish base evaluated the INNER group's
        by-hash entries against the OUTER group's `live_checksums` — which
        never contain them, by construction. The inner group then correctly
        declined to delete them, but they were already on the list, and the
        same bundle's own manifest still named them present: `apply.py`
        deleted a file its manifest listed, so `--verify` failed on every
        bundle of a nested-base network and by-hash eroded there exactly as
        it had everywhere before. `exclude` is therefore tested FIRST, and it
        applies to by-hash paths as much as to plain ones.

        A by-hash entry's filename IS its checksum, so its retention rule is
        exact and needs no directory listing: delete it only when that
        checksum is absent from `live_checksums`. This deliberately does NOT
        reproduce `TreeSink`'s `_BY_HASH_KEEP_STALE = 2` (keep a couple of
        stale generations for in-flight clients) — that rule sorts a real
        directory by mtime, and a bundle has no mtime information about the
        far side's files at all. Deleting strictly on "checksum not live" is
        still strictly safer than the bug this replaces, which deleted the
        *live* generation.
        """
        if not self._prune:
            return 0, 0
        prefix = base + "/" if base else ""
        added = 0
        for rel in self._prev:
            if not rel.startswith(prefix):
                continue
            # BEFORE the by-hash branch — see the docstring. A nested base's
            # paths are another group's business whatever shape they have.
            if any(rel == x or rel.startswith(x + "/") for x in exclude):
                continue
            if "by-hash" in rel.split("/"):
                checksum = rel.rsplit("/", 1)[-1]
                if checksum not in live_checksums:
                    self.deleted.append(rel)
                    added += 1
                continue
            if rel not in keep:
                self.deleted.append(rel)
                added += 1
        # Bundle deletions are far-side path removals, not local bytes freed —
        # the previous manifest carries no size, only checksum. `bytes_pruned`
        # is always 0 for this sink; see the Sink protocol docstring.
        return added, 0

    def prune_stale_bases(self, live_bases: set[str]) -> tuple[int, int]:
        """See the Sink protocol. The bundle equivalent of `TreeSink`'s
        subtree sweep: `finalize_group` only ever iterates `prev` under the
        CURRENT group's prefix, so a path belonging to a base this run
        produced no group for was matched by nothing at all."""
        if not self._prune:
            return 0, 0
        ancestors = base_ancestors(live_bases)
        added = 0
        for rel in self._prev:
            top = rel.split("/", 1)[0]
            if top in NON_BASE_DIRS or rel in NON_BASE_FILES:
                continue
            if rel in self._seen:
                # This run produced it, so whatever base it belongs to is
                # live even if it isn't spelled in `live_bases` (changelogs
                # and UPGRADE.md take this route on an older layout).
                continue
            rel_dir = rel.rsplit("/", 1)[0] if "/" in rel else ""
            if not rel_dir:
                continue
            if _is_stale_base(rel_dir, live_bases, ancestors):
                self.deleted.append(rel)
                added += 1
        return added, 0

    def close(self) -> None:
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        # ── Pre-flight: a row whose pool blob can't be streamed is dropped
        # from the plan BEFORE the manifest that describes the plan is
        # written. This used to happen inside the write loop instead, which
        # made every such failure unrecoverable on the far side:
        # `file_count` was computed up front from the unfiltered lists, the
        # loop then `continue`d past the bad row, and the tarball STILL
        # landed and the ledger manifest STILL advanced. The far side
        # correctly refused (`written != declared` → EXIT_TOOLING, so
        # `.mmw-state` never moved) while the producing side's ledger had
        # moved — every subsequent delta then failed EXIT_OUT_OF_ORDER
        # forever, and the skipped file sat in manifest N with a real
        # checksum so no delta would ever resend it either. Recovery was a
        # multi-TB `--full`.
        #
        # Dropping the row instead keeps every side consistent: the tar,
        # `file_count` and the manifest all agree, the far side applies
        # cleanly, and — because the manifest no longer claims the path —
        # the NEXT ordinary delta bundle resends it on its own. The failure
        # is still loud: it is in `self.errors`, which `bundle_network`
        # folds into `BundleResult`, so `mmw bundle` exits 1.
        send: list[tuple[str, Path, int, str]] = []
        dropped: set[str] = set()
        for rel, src, size, checksum in self._send:
            # Verified BEFORE any tar write: TarFile.addfile() writes the
            # entry's header (and whatever chunks it manages to read)
            # straight to the compressed stream before a short read can
            # raise — so catching that failure mid-loop would leave
            # tarfile's offset bookkeeping out of sync with bytes already
            # flushed, corrupting every entry after it. A pool blob is
            # content-addressed and shouldn't change, but the size recorded
            # at link() time must still be checked against the file on disk.
            try:
                actual_size = src.stat().st_size
            except OSError as e:
                self.errors.append(f"{rel}: {e}")
                dropped.add(rel)
                continue
            if actual_size != size:
                self.errors.append(
                    f"{rel}: pool blob is {actual_size} bytes, "
                    f"expected {size} — skipped rather than "
                    "write a truncated tar entry")
                dropped.add(rel)
                continue
            send.append((rel, src, size, checksum))
        if dropped:
            self.manifest_rows = [(r, c) for r, c in self.manifest_rows
                                  if r not in dropped]
            for rel, _src, size, _cs in self._send:
                if rel in dropped:
                    self.sent -= 1
                    self.sent_bytes -= size
            self._send = send

        # `_send`/`_generated` are final the instant link()/write() stop being
        # called, so exactly how many real files (never the fixed .mmw/
        # control members) THIS bundle's tar will contain is known before any
        # archive I/O runs — declared in the manifest header apply.py reads
        # first, so it can tell "this bundle never meant to include that path"
        # (a delta bundle's normal, correct shape) apart from "this bundle's
        # tar got cut short" (see ManifestHeader.file_count's docstring).
        header = replace(self.header, file_count=len(send) + len(self._generated))
        # The ledger must record the header that actually shipped, not the
        # one bundle_network constructed — `file_count` only exists on this
        # one, so a ledger written from the original was not a faithful copy
        # of what the far side received.
        self.written_header = header
        # The manifest is written to a temp file first: it goes in as the
        # first tar member and tarfile needs its size up front.
        tmp_manifest = self.out_path.parent / (self.out_path.name + ".manifest.tmp")
        write_manifest(tmp_manifest, header, self.manifest_rows)

        # `sent`/`sent_bytes` are final the instant link()/write() stop being
        # called (both only ever append to in-memory lists), so the totals
        # for a determinate progress bar are known before any archive I/O
        # runs at all — the UI need never show an indeterminate spinner.
        if self._progress:
            self._progress("", f"archive-start:{self.sent}:{self.sent_bytes}")
        files_written = 0
        # Bytes of *source* data fed into the tar so far — comparable to
        # `self.sent_bytes` (both uncompressed), unlike `counting.written`
        # (the compressed bytes actually hitting disk). Comparing those two
        # directly as "X / TOTAL" is comparing different units and can never
        # cleanly reach 100%; this is what makes an apples-to-apples fraction
        # (and a live compression ratio) possible instead.
        raw_written = 0
        self._archive_last_report = time.monotonic()

        tmp_out = self.out_path.with_suffix(self.out_path.suffix + ".tmp")
        try:
            with open(tmp_out, "wb") as raw_fh:
                counting = _CountingWriter(raw_fh)
                # Uncompressed: `tarfile` writes straight to the counting
                # wrapper, no zstd layer at all — not "zstd at level 0",
                # which still carries frame overhead and wouldn't produce a
                # plain, universally-readable .tar. `nullcontext` keeps this
                # a one-branch difference rather than duplicating everything
                # below it for the two cases.
                if self._compress:
                    cctx = zstandard.ZstdCompressor(level=self._level, threads=-1)
                    stream_ctx = cctx.stream_writer(counting)
                else:
                    stream_ctx = contextlib.nullcontext(counting)
                with stream_ctx as zw:
                    with tarfile.open(fileobj=zw, mode="w|") as tar:
                        manifest_bytes = tmp_manifest.read_bytes()
                        self._add_file(tar, ".mmw/manifest.jsonl.zst", manifest_bytes)
                        files_written += 1
                        raw_written += len(manifest_bytes)
                        if self._prune:
                            body = "\n".join(sorted(self.deleted))
                            if body:
                                body += "\n"
                            self._add_file(tar, ".mmw/delete.list", body.encode())
                            files_written += 1
                            raw_written += len(body.encode())
                        self._add_file(tar, ".mmw/apply.py", self._apply_script,
                                       mode=0o755)
                        files_written += 1
                        raw_written += len(self._apply_script)
                        for name, data in sorted(self._tls_files.items()):
                            self._add_file(tar, f"{TLS_DIR}/{name}", data,
                                           mode=0o600 if name.endswith(".key") else 0o644)
                            files_written += 1
                            raw_written += len(data)
                        for rel, data in self._generated.items():
                            self._add_file(tar, rel, data)
                            files_written += 1
                            raw_written += len(data)
                        by_checksum: dict[str, str] = {}
                        for rel, src, size, checksum in send:
                            first = by_checksum.get(checksum) if checksum else None
                            if first is not None:
                                ti = tarfile.TarInfo(rel)
                                ti.type = tarfile.LNKTYPE
                                ti.linkname = first
                                ti.mode = 0o644
                                tar.addfile(ti)
                                files_written += 1
                                # A hardlink entry carries no payload bytes of
                                # its own — the size was already counted for
                                # the first occurrence of this checksum.
                                self._maybe_report_archive_progress(
                                    rel, files_written, raw_written, counting.written)
                                continue
                            is_large = size >= _ANNOUNCE_LARGE_FILE_BYTES
                            if self._progress and is_large:
                                self._progress(rel, f"archive-large-file:{size}")
                            # Every pre-detectable failure was filtered out
                            # above; a blob that vanishes between that stat
                            # and this open did so DURING the write, with
                            # the manifest already in the tar declaring it.
                            # Skipping it here would ship a tar the far side
                            # refuses forever, so this aborts instead: the
                            # exception propagates, `tmp_out` is discarded by
                            # the `finally` below, and `bundle_network` never
                            # reaches the ledger write.
                            fh = open(src, "rb")
                            ti = tarfile.TarInfo(rel)
                            ti.size = size
                            ti.mode = 0o644
                            with fh:
                                # Size is already verified — a failure here
                                # means the blob changed under us between the
                                # stat() above and this read. Let it
                                # propagate: close() aborts and tmp_out is
                                # discarded (finally, below), which is safer
                                # than risking a silently misaligned archive.
                                # Wrapping `fh` for a large file only costs a
                                # `.read()` indirection — `tarfile` only ever
                                # calls `.read()` on it, so nothing else here
                                # needs to know or care which one it got.
                                stream = (_ProgressFile(fh, size, self._progress, rel)
                                         if self._progress and is_large else fh)
                                tar.addfile(ti, stream)
                            files_written += 1
                            raw_written += size
                            self._maybe_report_archive_progress(
                                rel, files_written, raw_written, counting.written)
                            # Only a successfully-written entry may become the
                            # hardlink target for a later duplicate checksum —
                            # registering it earlier would let a failed write
                            # leave a hardlink entry pointing at nothing.
                            if checksum:
                                by_checksum[checksum] = rel
            # `counting.written` is only trustworthy once the compressor and
            # the file handle above have both been flushed and closed — read
            # it after the `with open(...)` block exits, not inside it.
            if self._progress:
                self._progress("", f"archive-done:{files_written}:{raw_written}:{counting.written}")
            tmp_out.replace(self.out_path)
        finally:
            tmp_manifest.unlink(missing_ok=True)
            tmp_out.unlink(missing_ok=True)

    def _maybe_report_archive_progress(
        self, rel: str, files_written: int, raw_written: int, compressed_written: int
    ) -> None:
        """Throttled to `_ARCHIVE_REPORT_INTERVAL` — `rel` is whichever file
        happened to complete at the moment a report is allowed through, not
        every file. Unlike `link()`'s per-file log during the (in-memory, no
        natural rate limit) enumeration phase, this loop does real disk I/O:
        a burst of cache-hit small files could still fire fast enough to
        flood a log kept at full per-file fidelity, so "what's being archived
        right now" rides the same throttle as the byte/file counters instead
        of adding a second, unthrottled path to the same UI.
        """
        if not self._progress:
            return
        now = time.monotonic()
        if now - self._archive_last_report < _ARCHIVE_REPORT_INTERVAL:
            return
        self._archive_last_report = now
        self._progress(rel, f"archive-progress:{files_written}:{raw_written}:{compressed_written}")

    @staticmethod
    def _add_file(tar: tarfile.TarFile, name: str, data: bytes,
                  mode: int = 0o644) -> None:
        ti = tarfile.TarInfo(name)
        ti.size = len(data)
        ti.mode = mode
        tar.addfile(ti, io.BytesIO(data))
