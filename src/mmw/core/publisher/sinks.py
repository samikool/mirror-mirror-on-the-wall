"""Destination sinks: one publish pipeline, two kinds of destination.

`publish_group` enumerates from the pool and orders pkg → meta → release; a
Sink decides what "put this file there" means. `TreeSink` hardlinks into a
directory (a live-served mirror); `PointsSink` streams into a plain tar that
crosses an airgap.

`rel` is always network-root-relative and POSIX:
    archive.ubuntu.com/ubuntu/dists/noble/Release

**The `progress` callback a Sink is constructed with follows the same rule:**
every call is `progress(rel: str, status: str)` — a sink-relative POSIX
string, never a `Path`. A bundle sink has no destination path to hand back at
all, so a contract that assumed one could not survive it. The one production
consumer, `TransferView.record` (`tui/transfer.py`), expects a
`str` and will raise on a `Path`.
"""
import hashlib
import os
from stat import S_ISREG
import time
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from mmw.core import disk_io
from mmw.core.publisher.manifest import ManifestHeader, unchanged, write_manifest
from mmw.core.publisher.nginx import ROOT_PLACEHOLDER

_SCAN_REPORT_EVERY = 5000
_BY_HASH_KEEP_STALE = 2
_ARCHIVE_REPORT_INTERVAL = 0.25  # seconds — matches the TUI's own EMA sample rate
# A file this size or larger gets its own immediate, unthrottled "now
# writing X" announcement the instant it starts — not just whenever it
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
# the dist-upgrade step; `.mmw/` is the far side's staging area and `.mmw-state`
# its ledger cursor — deleting either from a far side would be catastrophic.
# `pypi.org` is rendered in place from `pypi-data/` (render_derived), never a sink base.
NON_BASE_DIRS = frozenset({"nginx", "keys", "changelogs.ubuntu.com", ".mmw", "vscode-gallery",
                           "pypi-data", "pypi.org"})

# Where a `certs` network's certificate and key land on the far side:
# under `.mmw/`, which no generated server block serves (each one's root is a
# hostname directory). Fixed control members under `.mmw/`, not
# manifest payload — so they ride every bundle, delta or full, and sit near
# the front of the archive rather than in the tail a cut-short copy loses.
TLS_DIR = ".mmw/tls"
NON_BASE_FILES = frozenset({"UPGRADE.md", "PYPI.md", ".mmw-state", "apply.py", "sign.py", "make-ca.py"})

# Where TreeSink stages content before renaming it onto a live-served path.
# Never inside a `mirror_base`, so nginx can never serve it — the same
# reasoning as the far side's own `{root}/.mmw/tmp/`.
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


class _ProgressFile:
    """Wraps a file object being read; reports percentage-through progress on
    a large file as `tarfile.addfile()`'s internal copy loop calls `.read()`.

    Only used for files at or above `_ANNOUNCE_LARGE_FILE_BYTES` — a small
    file finishes fast enough that the per-file sampling in
    `_maybe_report_archive_progress` already covers it; a multi-GB one can
    otherwise sit at "writing…" with no sense of how far through it is.
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

        Preserve the base directory itself, even when it becomes empty.

        Returns (files_pruned, bytes_pruned) for THIS group only. A sink may be
        reused across every group in one network publish (`TreeSink` is, so
        by-hash retirement and destination pruning share one instance across a
        multi-repository network), so this cannot be a running total —
        `publish_group` adds the return value straight onto that group's own
        `MirrorPublishResult`. A bundle sink returns
        (len(deletions recorded for this group), 0).
        """
        ...

    def drop_if_empty(self, base: str) -> None:
        """Remove an empty directory explicitly; never remove the publish root.

        Callers only invoke this when pruning. Non-tree sinks have no
        directories to remove; their file deletions are handled separately.
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

    def __init__(self, root: Path, progress=None, stop_event=None, *,
                 prev: dict[str, str] | None = None, recheck: bool = False):
        self.root = root
        # The filesystem work runs on this job's lanes, closed with the sink.
        self.job = disk_io.DiskJob("publish", stop_event=stop_event)
        self._progress = progress
        self._stop_event = stop_event
        self.pruned = 0
        self.pruned_bytes = 0
        self.errors: list[str] = []
        # The tree's record from its last publish (`manifest.RECORD_PATH`), or
        # None to check the filesystem as publish always did. With a record,
        # an unchanged path costs nothing and pruning reads the record, not
        # the tree; `recheck` ignores it.
        self._prev = None if recheck else prev
        self._recheck = recheck
        self._rows: dict[str, str] = {}
        # Sorted once: each group's records are one bisected slice — VS Code
        # finalizes once per publisher host, tens of thousands of groups.
        self._prev_sorted = sorted(self._prev) if self._prev is not None else []
        self._bh_dirs: set[str] = set()      # by-hash dirs this run placed into

    @property
    def recheck(self) -> bool:
        """`--recheck`: providers recheck what they would otherwise reuse."""
        return self._recheck

    @property
    def manifest_rows(self) -> list[tuple[str, str]]:
        """(path, checksum) of everything this run placed — the new record."""
        return list(self._rows.items())

    @property
    def tmp_dir(self) -> Path:
        return self.root.joinpath(*_TMP_SUBDIR)

    def link(self, src: Path, rel: str, size: int, checksum: str, kind: str) -> str:
        if "/by-hash/" in f"/{rel}":
            self._bh_dirs.add(rel.rsplit("/", 1)[0])
        if unchanged(self._prev, rel, checksum, self._recheck):
            self._rows[rel] = checksum
            return "skipped"
        return self._settle(rel, checksum, self._place(src, rel))

    def link_many(self, items):
        """`link` for each (src, rel, size, checksum, kind), the filesystem
        work on the job's write lane (`disk_write_threads`). A cold publish
        waits on inode and directory reads, one at a time; several in flight
        let the disk order them (2.5x on the HDD, measured). Yields (status,
        error or None) per item, in item order, as soon as that item is done
        — collecting the whole run first made progress arrive in bursts of a
        run. The record and `errors` are settled here, on the caller's
        thread. A stop ends the run after the rows already placed."""
        work = [(src, rel) for src, rel, _size, checksum, _kind in items
                if not unchanged(self._prev, rel, checksum, self._recheck)]
        placed = (outcome for _w, outcome in self.job.meta_map(lambda w: self._place(*w), work))
        for _src, rel, _size, checksum, _kind in items:
            if "/by-hash/" in f"/{rel}":
                self._bh_dirs.add(rel.rsplit("/", 1)[0])
            if unchanged(self._prev, rel, checksum, self._recheck):
                self._rows[rel] = checksum
                yield "skipped", None
                continue
            outcome = next(placed, None)
            if outcome is None:
                return                      # stopped: the rest were never placed
            if isinstance(outcome, Exception) and not isinstance(outcome, OSError):
                raise outcome               # a bug, not a filesystem failure: as before the lane
            status = self._settle(rel, checksum, outcome)
            yield status, (self.errors[-1] if status == "error" else None)

    def _place(self, src: Path, rel: str) -> "str | OSError":
        """The filesystem half of `link` — safe on any thread."""
        try:
            return disk_io.ops.place(src, self.root / rel, self.tmp_dir)
        except OSError as e:
            return e

    def _settle(self, rel: str, checksum: str, outcome: "str | OSError") -> str:
        if isinstance(outcome, OSError):
            self.errors.append(f"{rel}: {outcome}")
            return "error"
        self._rows[rel] = checksum
        return outcome

    def write(self, rel: str, data: bytes) -> None:
        # A TreeSink destination is live-served. The nginx root placeholder
        # must always have already been substituted for a real path by the
        # caller (`str(network.root)`, publisher/__init__.py and bundle.py) —
        # an unsubstituted placeholder reaching disk here is a bug in that
        # caller, not something this sink should silently write out. See
        # nginx.py's own docstring: "An unsubstituted placeholder reaching
        # disk on either side is a bug."
        if rel.startswith("nginx/") and rel.endswith(".conf") and ROOT_PLACEHOLDER.encode() in data:
            raise ValueError(
                f"{rel}: unsubstituted {ROOT_PLACEHOLDER} would have been "
                "written to a live-served destination"
            )
        checksum = hashlib.sha256(data).hexdigest()
        self._rows[rel] = checksum
        if unchanged(self._prev, rel, checksum, self._recheck):
            return
        # Staged-then-renamed like every other write into a live-served tree:
        # a client fetching nginx/mirror.conf or UPGRADE.md mid-publish gets
        # the old file or the new one, never a half-written one.
        self.job.atomic_write(self.root / rel, data, tmp_dir=self.tmp_dir)

    def finalize_group(self, base: str, keep: set[str],
                       live_checksums: set[str], exclude: set[str]) -> tuple[int, int]:
        if self._prev is not None:
            return self._finalize_from_record(base, keep, live_checksums, exclude)
        dst_root = self.root / base if base else self.root
        if not disk_io.ops.exists(dst_root):
            return 0, 0
        group_pruned = 0
        group_pruned_bytes = 0
        bh_dirs: set[Path] = set()
        checked = 0
        # `progress` always receives a sink-relative POSIX str, never a Path —
        # a bundle sink has no destination path at all, so the callback
        # contract cannot assume one. `base` stands in for `dst_root` here.
        if self._progress:
            self._progress(base, "prune-scan-start")
        # os.walk (scandir) rather than rglob: kept files — the overwhelming
        # majority — cost no stat at all, and whole subtrees are skipped by
        # pruning dirnames instead of testing every file under them.
        seen_dirs: list[Path] = []
        stale: list[str] = []
        for dirpath, dirnames, filenames in disk_io.ops.walk(dst_root):
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
                if rel not in keep:
                    stale.append(rel)
        # The walk only lists; the unlinks go through the job's write lane,
        # settled here in walk order.
        for rel, size in self.job.meta_map(lambda r: disk_io.ops.remove(self.root / r), stale):
            if isinstance(size, int):
                group_pruned += 1
                group_pruned_bytes += size
                if self._progress:
                    self._progress(rel, "pruned")
        bh_pruned = 0
        for bh_dir in bh_dirs:
            bh_pruned += _prune_by_hash_dir(bh_dir, live_checksums, self.job)
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

    def _finalize_from_record(self, base: str, keep: set[str],
                              live_checksums: set[str], exclude: set[str]) -> tuple[int, int]:
        """`finalize_group` trusting the record: what it lists under `base`
        and `keep` lacks is pruned — no walk of the tree. by-hash companions
        keep their own retention rule, in the by-hash dirs the record names."""
        import bisect
        prefix = f"{base}/" if base else ""
        skip = tuple(f"{x}/" for x in exclude)
        pruned = pruned_bytes = 0
        bh_dirs: set[Path] = set()
        emptied: set[Path] = set()
        records = self._prev_sorted
        start = bisect.bisect_left(records, prefix)
        stale: list[str] = []
        for i in range(start, len(records)):
            rel = records[i]
            if not rel.startswith(prefix):
                break
            if rel in keep or (skip and rel.startswith(skip)):
                continue
            if "/by-hash/" in f"/{rel}":
                bh_dirs.add((self.root / rel).parent)
                continue
            stale.append(rel)
        done = 0
        for rel, size in self.job.meta_map(lambda r: disk_io.ops.remove(self.root / r), stale):
            done += 1
            if size is None:
                continue                  # already gone: nothing to record
            if isinstance(size, Exception):
                # Still there (permissions, a busy mount): keep it in the
                # record so the next publish tries again, rather than losing
                # sight of it and serving it forever.
                self._rows[rel] = self._prev[rel]
                continue
            pruned += 1
            pruned_bytes += size
            emptied.add((self.root / rel).parent)
            if self._progress:
                self._progress(rel, "pruned")
        for rel in stale[done:]:          # stopped before these: still served
            self._rows[rel] = self._prev[rel]
        for rel_dir in self._bh_dirs:
            if (rel_dir + "/").startswith(prefix) and not (skip and (rel_dir + "/").startswith(skip)):
                bh_dirs.add(self.root / rel_dir)
        bh_pruned = sum(_prune_by_hash_dir(d, live_checksums, self.job)
                        for d in bh_dirs if disk_io.ops.is_dir(d))
        stop = self.root / base if base else self.root
        for d in sorted(emptied, key=lambda p: len(p.parts), reverse=True):
            while d != stop and stop in d.parents:
                try:
                    disk_io.ops.rmdir(d)
                except OSError:
                    break
                d = d.parent
        self.pruned += pruned
        self.pruned_bytes += pruned_bytes
        if self._progress and bh_pruned:
            self._progress(base, f"by-hash:{bh_pruned}")
        return pruned, pruned_bytes

    def drop_if_empty(self, base: str) -> None:
        dst = self.root / base
        if dst == self.root:
            return
        try:
            disk_io.ops.rmdir(dst)
        except OSError:
            pass                        # not empty, or already gone

    def prune_stale_bases(self, live_bases: set[str]) -> tuple[int, int]:
        """See the Sink protocol. Walks down from the root only as far as the
        live bases' own ancestors, so a whole dropped repository costs one
        `rmtree` and a healthy tree costs a handful of `scandir` calls."""
        if not disk_io.ops.is_dir(self.root):
            return 0, 0
        ancestors = base_ancestors(live_bases)
        removed = 0
        removed_bytes = 0
        stack = [""]
        while stack:
            rel_dir = stack.pop()
            here = self.root / rel_dir if rel_dir else self.root
            try:
                entries = list(disk_io.ops.iterdir(here))
            except OSError:
                continue
            for entry in entries:
                # A symlink is never something publish created, and following
                # one out of the tree with rmtree is exactly the mistake a
                # destructive sweep must not be capable of making.
                if disk_io.ops.is_symlink(entry) or not disk_io.ops.is_dir(entry):
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
                stop = self.job.stop_event
                if stop is not None and stop.is_set():
                    stack.clear()
                    break               # stopped: no base goes half-counted
                # The size sum's lstats go through the job's read lane; the
                # removal itself stays one rmtree per dropped base.
                for _f, st in self.job.read_map(disk_io.ops.lstat, disk_io.ops.rglob(entry, "*")):
                    if isinstance(st, os.stat_result) and S_ISREG(st.st_mode):
                        removed_bytes += st.st_size
                        removed += 1
                disk_io.ops.rmtree(entry, ignore_errors=True)
                if self._progress:
                    self._progress(rel, "pruned-base")
        self.pruned += removed
        self.pruned_bytes += removed_bytes
        return removed, removed_bytes

    def close(self) -> None:
        self.job.close()
        # Debris from a killed publish: not servable (nothing under
        # `{root}/.mmw/` is inside any mirror_base, so no nginx site roots
        # reach it), but it would accumulate forever otherwise. One listdir.
        try:
            for entry in disk_io.ops.iterdir(self.tmp_dir):
                try:
                    disk_io.ops.unlink(entry)
                except OSError:
                    pass
        except OSError:
            pass


def _next_open(opened, job: "disk_io.DiskJob"):
    """The next blob's handle from a `stream_open`. Its open failing is the
    blob vanishing mid-bundle — raised, so the bundle aborts; the stream
    ending early is a stop."""
    try:
        _path, fh = next(opened)
    except StopIteration:
        raise InterruptedError("bundle stopped — nothing was written") from None
    if isinstance(fh, Exception):
        raise fh
    return fh


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
            disk_io.ops.rmdir(d)
            removed += 1
        except OSError:
            pass
    return removed


def _prune_by_hash_dir(bh_dir: Path, live_checksums: set[str], job: "disk_io.DiskJob") -> int:
    """Retire stale by-hash generations, keeping the newest few for in-flight
    clients. An entry's name IS its checksum, so staleness is exact: not in
    the current refs set. (nlink can't be used here — the pool keeps old
    blobs' link counts above 1.) One listing; the stats and unlinks go
    through `job`'s lanes."""
    try:
        names = [e for e in disk_io.ops.iterdir(bh_dir) if e.name not in live_checksums]
    except OSError:
        return 0
    stale = [(st.st_mtime, e) for e, st in job.stat(names)
             if isinstance(st, os.stat_result) and S_ISREG(st.st_mode)]
    stale.sort(reverse=True)
    return sum(1 for _e, size in job.remove([e for _, e in stale[_BY_HASH_KEEP_STALE:]])
               if isinstance(size, int))


class PointsSink:
    """A bundle of timeline points (manifest format 3): rows are (path,
    checksum) with the points holding them; each file goes once, named by
    its checksum, and only if `sent` (what this network was sent before)
    lacks it. One plain tar (no compression: the payload is packages,
    already compressed), or the members through `writer`.

    `link_point` is a repository's file of point `k`; `link`/`write` are
    network-level (no repository). Nothing is written until `close()`: the
    manifest goes first and is complete only once every row is known.
    `sent_checksums`, once closed: what the tar carried — the caller records
    it only after this returns."""

    def __init__(self, out_path: Path, header: ManifestHeader, *, sent: set[str], progress=None,
                 stop_event=None, writer=None, scratch_dir: Path | None = None,
                 signing_files: dict[str, bytes] | None = None):
        self.out_path = out_path
        self.header = header
        # Where the members go (`members.MemberWriter` factory); by default
        # one plain tar at `out_path`, written beside it and renamed in.
        self._writer = writer
        # Where the manifest is staged before it becomes the first member
        # (tarfile needs its size up front): beside the bundle by default.
        self._scratch_dir = scratch_dir
        self.job = disk_io.DiskJob("bundle", stop_event=stop_event)
        # name → armoured private key, written as `.mmw/signing/{name}.asc`
        # (0600): the keys a network carries to its far side.
        self._signing_files = dict(signing_files or {})
        self._progress = progress
        self._sent_before = set(sent)
        self._rows: dict[tuple[str, str], list] = {}          # (rel, checksum) → [repo, size, {k}]
        self._carry: dict[str, tuple[Path | None, int, bytes | None]] = {}   # checksum → (src, size, data)
        self.sent_checksums: set[str] = set()
        # The header actually written, complete with `file_count`; the
        # ledger keeps THIS, a faithful record of what shipped.
        self.written_header: ManifestHeader | None = None
        self.errors: list[str] = []
        self.archive_sha256: str | None = None     # the written tar's, when it is a file

    def link_point(self, src: Path, rel: str, size: int, checksum: str, kind: str, repo: str | None,
                   k: int | None) -> str:
        row = self._rows.setdefault((rel, checksum), [repo, size, set()])
        if k is not None:
            row[2].add(k)
        if checksum not in self._sent_before and checksum not in self._carry:
            self._carry[checksum] = (Path(src), size, None)
        return "linked"

    def link(self, src: Path, rel: str, size: int, checksum: str, kind: str) -> str:
        return self.link_point(src, rel, size, checksum, kind, None, None)

    def write(self, rel: str, data: bytes) -> None:
        checksum = hashlib.sha256(data).hexdigest()
        self._rows.setdefault((rel, checksum), [None, len(data), set()])
        if checksum not in self._sent_before and checksum not in self._carry:
            self._carry[checksum] = (None, len(data), data)

    @property
    def sent(self) -> int:
        """Files this bundle's tar carries: each checksum once."""
        return len(self._carry)

    @property
    def sent_bytes(self) -> int:
        return sum(size for _src, size, _data in self._carry.values())

    def tagged_rows(self):
        return [(rel, cs, repo, size, tuple(sorted(k)))
                for (rel, cs), (repo, size, k) in sorted(self._rows.items())]

    def close(self) -> None:
        try:
            self._close()
        finally:
            self.job.close()

    def _close(self) -> None:
        from mmw.core.publisher.manifest import blob_member
        from mmw.core.verify import blob_stats
        if self._writer is None:
            disk_io.ops.mkdir(self.out_path.parent, parents=True, exist_ok=True)
        # Every pool blob must be exactly what was recorded, or nothing is
        # written — checked BEFORE any tar write (a short read mid-member
        # would leave a misaligned archive), on the job's read lane.
        pooled = [(cs, src, size) for cs, (src, size, data) in sorted(self._carry.items()) if data is None]
        total = len(pooled)
        report = ((lambda done: self._progress("", f"preflight:{done}:{total}")) if self._progress else None)
        bad: list[str] = []
        for (cs, _src, size), st in blob_stats(pooled, self.job, path_of=lambda r: r[1], on_progress=report):
            if isinstance(st, OSError):
                bad.append(f"{cs}: {st}")
            elif st.st_size != size:
                bad.append(f"{cs}: pool blob is {st.st_size} bytes, expected {size}")
        stop = self.job.stop_event
        if stop is not None and stop.is_set():
            raise InterruptedError("bundle stopped — nothing was written")
        if bad:
            self.errors.extend(bad)
            raise ValueError(f"{len(bad)} file(s) are damaged in the pool, so nothing was bundled "
                             f"(first: {bad[0]}). `mmw verify --repair` drops them and the next "
                             "`mmw sync` downloads them again.")
        header = replace(self.header, file_count=len(self._carry))
        self.written_header = header
        scratch = self._scratch_dir or self.out_path.parent
        disk_io.ops.mkdir(scratch, parents=True, exist_ok=True)
        tmp_manifest = scratch / (self.out_path.name + ".manifest.tmp")
        write_manifest(tmp_manifest, header, self.tagged_rows())
        if self._progress:
            self._progress("", f"archive-start:{self.sent}:{self.sent_bytes}")
        # A member is named by its checksum; progress names a path it is
        # served at, which means something to the person watching.
        shown: dict[str, str] = {}
        for rel, cs in sorted(self._rows):
            shown.setdefault(cs, rel)
        writer = self._writer() if self._writer is not None else self._tar_writer()
        committed = False
        files_written, raw_written = 0, 0
        self._archive_last_report = time.monotonic()
        try:
            with ExitStack() as stack:
                writer.add_bytes(".mmw/manifest.jsonl", disk_io.ops.read_bytes(tmp_manifest))
                for name, data in sorted(self._signing_files.items()):
                    writer.add_bytes(f".mmw/signing/{name}.asc", data, mode=0o600)
                for cs, (src, size, data) in sorted(self._carry.items()):
                    if data is not None:
                        writer.add_bytes(blob_member(cs), data)
                        files_written += 1
                        raw_written += size
                        self._maybe_report_archive_progress(shown.get(cs, blob_member(cs)), files_written,
                                                            raw_written)
                # Pool blobs are opened ahead on the job's read lane while this
                # thread writes the tar in order (`disk_readahead`).
                opened = self.job.stream_open([src for _cs, src, _size in pooled])
                stack.callback(opened.close)        # handles opened ahead close on abort
                for cs, src, size in pooled:
                    if stop is not None and stop.is_set():
                        raise InterruptedError("bundle stopped — nothing was written")
                    member, name = blob_member(cs), shown.get(cs, blob_member(cs))
                    large = size >= _ANNOUNCE_LARGE_FILE_BYTES
                    if self._progress and large:
                        self._progress(name, f"archive-large-file:{size}")
                    # A blob gone between the pre-flight and here vanished
                    # DURING the write, with the manifest already declaring
                    # it: this aborts, and nothing is recorded as sent.
                    fh = _next_open(opened, self.job)
                    with fh:
                        stream = _ProgressFile(fh, size, self._progress, name) if self._progress and large else fh
                        writer.add_blob(member, stream, size, src=Path(src))
                    files_written += 1
                    raw_written += size
                    self._maybe_report_archive_progress(name, files_written, raw_written)
            if self._progress:
                self._progress("", f"archive-done:{files_written}:{raw_written}")
            writer.commit()
            committed = True
            self.archive_sha256 = getattr(writer, "sha256", None)
            self.sent_checksums = set(self._carry)
        finally:
            disk_io.ops.unlink(tmp_manifest, missing_ok=True)
            if not committed:
                writer.abort()

    def _tar_writer(self):
        from mmw.core.publisher.members import TarMembers
        tmp_out = self.out_path.with_suffix(self.out_path.suffix + ".tmp")
        return TarMembers(disk_io.ops.open_file(tmp_out, "wb"), final_path=self.out_path, tmp_path=tmp_out)

    def _maybe_report_archive_progress(self, rel: str, files_written: int, raw_written: int) -> None:
        """Throttled to `_ARCHIVE_REPORT_INTERVAL` — `rel` is whichever file
        completed when a report was allowed through, not every file."""
        if not self._progress:
            return
        now = time.monotonic()
        if now - self._archive_last_report < _ARCHIVE_REPORT_INTERVAL:
            return
        self._archive_last_report = now
        self._progress(rel, f"archive-progress:{files_written}:{raw_written}")
