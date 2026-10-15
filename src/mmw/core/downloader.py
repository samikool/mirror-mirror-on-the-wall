import asyncio
import collections
import concurrent.futures
import functools
import hashlib
import os
import posixpath
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from mmw.core.item_progress import ItemCounts
    from mmw.core.download_coordinator import DownloadCoordinator

import httpx

from mmw.core.http import request_scope, retrying_client

from mmw.core.pool import (
    KIND_META,
    KIND_PKG,
    KIND_RELEASE,
    Pool,
    acquire_sync_lock,
    release_sync_lock,
)
from mmw.providers.base import MetadataFile, PackageInfo


class Fetch(Enum):
    """Outcome of a metadata-file fetch."""
    OK = "ok"                     # content fetched and verified
    NOT_FOUND = "not_found"       # server 404 — file absent upstream
    ALREADY_DONE = "already_done" # blob already in the pool — nothing to fetch
    FAILED = "failed"             # network error or checksum mismatch after retries


# (outcome, content, reason) — reason is a short human string on FAILED, so an
# aborted index update says WHY, not just which files.
FetchResult = tuple[Fetch, bytes | None, str | None]


def is_safe_repo_path(repo_path: str) -> bool:
    """Reject upstream-supplied paths that could escape a published tree.

    repo_path comes straight from parsed metadata (Filename: fields, location
    hrefs, Release file paths). It is recorded in refs and later becomes a
    publish destination — a malicious repo must not be able to plant
    traversal there.
    """
    if not repo_path or repo_path.startswith("/") or "\\" in repo_path or "\x00" in repo_path:
        return False
    norm = posixpath.normpath(repo_path)
    return norm != ".." and not norm.startswith("../")


class Status(str, Enum):
    DOWNLOADED = "downloaded"
    POOLED = "pooled"       # already in pool — no download needed
    SKIPPED = "skipped"     # path already known from a previous sync's refs
    FAILED = "failed"


@dataclass
class DownloadResult:
    status: Status
    path: Path
    bytes_transferred: int = 0
    error: str = ""
    checksum: str = ""  # computed checksum, for providers with no upstream one


@dataclass
class MirrorSummary:
    mirror: str
    total: int = 0
    downloaded: int = 0
    pooled: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_transferred: int = 0
    errors: list[str] = field(default_factory=list)
    stopped: bool = False           # sync interrupted by user/teardown
    # Metadata phase outcome.  metadata_ok=False means the mirror's index was
    # NOT updated this sync: refs were not reconciled, so the previous
    # consistent snapshot remains in force.
    metadata_ok: bool = True
    metadata_errors: list[str] = field(default_factory=list)
    metadata_missing: int = 0       # index-listed files that 404'd upstream
    # True when the index was re-fetched mid-sync because upstream republished
    # while packages were downloading.  Informational, not a failure.
    metadata_resnapshotted: bool = False
    key_errors: list[str] = field(default_factory=list)  # GPG key sync failures
    # Deferred-writer failures (ENOSPC, `database is locked`, a corrupt DB).
    # `_DeferredWriter._commit` swallows these so one bad batch cannot kill the
    # writer thread mid-sync, and for a long time nothing read the list it put
    # them in — so up to 500 package rows per failed batch vanished, refs
    # pointed at packages the `refs ⋈ packages` join could not find, publish
    # shipped a short tree, and the sync exited 0.
    pool_write_errors: list[str] = field(default_factory=list)
    items: "ItemCounts | None" = None  # logical counts; file accounting above stays authoritative
    reconcile_seconds: float = 0.0
    timings: dict[str, float] = field(default_factory=dict)

    def timing_summary(self) -> str:
        return " · ".join(f"{phase} {seconds:.2f}s" for phase, seconds in self.timings.items())

    @property
    def ok(self) -> bool:
        return (
            not self.failed
            and self.metadata_ok
            and not self.stopped
            and not self.pool_write_errors
        )


ProgressCallback = Callable[[str, DownloadResult], None]
PhaseCallback = Callable[[str], None]
ChunkCallback = Callable[[int], None]

# One sync httpx client per thread — sync Client is not thread-safe to share.
_thread_local = threading.local()


def _thread_client() -> httpx.Client:
    if not hasattr(_thread_local, "client"):
        _thread_local.client = retrying_client(follow_redirects=True, timeout=60)
    return _thread_local.client


def fetch_packages_threaded(provider, mirror_config, status_callback=None) -> list[PackageInfo]:
    """Submit to an executor — calls provider.fetch_packages with the thread-local client."""
    return provider.fetch_packages(mirror_config, _thread_client(), status_callback=status_callback)


def fetch_metadata_threaded(provider, mirror_config, stop_event=None) -> list[MetadataFile]:
    """Submit to an executor — calls provider.fetch_metadata with the thread-local client."""
    with request_scope(stop_event):
        return provider.fetch_metadata(mirror_config, _thread_client())


def fetch_all_threaded(provider, mirror_config, status_callback=None,
                       history=None, stop_event=None) -> tuple[list[PackageInfo], list[MetadataFile]]:
    """Submit to an executor — calls provider.fetch_all with the thread-local client."""
    with request_scope(stop_event):
        return provider.fetch_all(mirror_config, _thread_client(), status_callback=status_callback,
                                  history=history)


_BY_HASH_RETRIES = 5
_BY_HASH_BACKOFF  = 2  # seconds; doubles each retry: 2 4 8 16 32 → ~62 s total

# Package downloads. Three attempts used to fire back to back with no delay at
# all, so all three landed inside the same overloaded second — the moment least
# likely to succeed — and a 404 for a package pulled from the archive mid-sync
# cost three round trips to confirm what the first response already said.
_PKG_RETRIES = 3
_PKG_BACKOFF = 1.0     # seconds; doubles: 1, 2
_STOP_POLL   = 0.25    # granularity at which a backoff notices a stop

# Headroom kept free when deciding whether a sync can fit. Other things share
# the filesystem (the SQLite WAL, another mirror syncing concurrently, the
# operator's own files), so filling it to the last byte is never the goal.
_DISK_MARGIN = 2 * 1024 * 1024 * 1024   # 2 GiB

# Yield to the event loop this often while emitting bulk POOLED progress so a
# huge mirror (100k+ pooled packages) doesn't freeze the TUI for seconds.
_PROGRESS_YIELD = 2_000


def _fetch_metadata_content(mf: MetadataFile, stop_event=None) -> FetchResult:
    with request_scope(stop_event):
        return _fetch_metadata_content_scoped(mf, stop_event)


def _fetch_metadata_content_scoped(mf: MetadataFile, stop_event=None) -> FetchResult:
    """Fetch and verify a metadata file's bytes. Does not touch the pool."""
    if stop_event is not None and stop_event.is_set():
        return Fetch.FAILED, None, "stopped"
    if mf.content is not None:
        content = mf.content
        if mf.expected_sha256 and hashlib.sha256(content).hexdigest() != mf.expected_sha256:
            return Fetch.FAILED, None, "checksum mismatch on pre-fetched content"
        return Fetch.OK, content, None

    if mf.fallback_url is not None:
        return _fetch_by_hash(mf, stop_event)

    client = _thread_client()
    try:
        resp = client.get(mf.url)
    except Exception as e:
        return Fetch.FAILED, None, type(e).__name__
    if resp.status_code == 404:
        return Fetch.NOT_FOUND, None, None
    if not resp.is_success:
        return Fetch.FAILED, None, f"http {resp.status_code}"
    content = resp.content
    if mf.expected_sha256 and hashlib.sha256(content).hexdigest() != mf.expected_sha256:
        return Fetch.FAILED, None, "checksum mismatch"
    return Fetch.OK, content, None


def _fetch_by_hash(mf: MetadataFile, stop_event=None) -> FetchResult:
    """Fetch a verified index file, preferring the by-hash URL.

    The by-hash response is hashed like any other.  It used to be taken on
    trust — "content IS the hash" — but that is an assumption about the
    *server*, and a content-addressed store exists precisely so we need not
    make one.  The cost is one `hashlib.sha256` over bytes already in memory;
    the risk it removes is permanent, because `_fetch_or_skip` short-circuits
    on `expected_sha256 in pooled`, so a single bad response (hostile mirror,
    misbehaving transparent proxy, a server that 200s a soft-error page) is
    pooled *under the expected checksum*, never re-fetched by any later sync,
    and deduplicated into every other mirror sharing that file.  A mismatch
    now falls through to the canonical URL, which is exactly the path a
    by-hash miss already takes.

    A by-hash miss has two very different causes and the canonical URL tells
    them apart immediately, so it is consulted *before* spending any backoff:

      * canonical 404 too  → the file is simply absent upstream (Ubuntu lists
        uncompressed Contents/Packages/dep11 variants it never publishes).
        NOT_FOUND, no retries — this used to burn ~62 s per file, 12 files per
        apt mirror.
      * canonical 200, hash matches → propagation lag on the by-hash dir only.
        Take it.
      * canonical 200, hash differs → either mid-publish lag (retry with
        backoff, the old behaviour) or our Release snapshot has gone stale
        because upstream republished during a long sync.  Retries settle the
        first; the caller re-snapshots for the second.
    """
    client = _thread_client()
    delay = _BY_HASH_BACKOFF
    last = "no attempt made"

    for attempt in range(_BY_HASH_RETRIES):
        if stop_event is not None and stop_event.is_set():
            return Fetch.FAILED, None, "stopped"
        try:
            resp = client.get(mf.url)
        except Exception as e:
            last = type(e).__name__
        else:
            if resp.is_success:
                content = resp.content
                if (
                    mf.expected_sha256
                    and hashlib.sha256(content).hexdigest() != mf.expected_sha256
                ):
                    # The by-hash URL served something that is not what its own
                    # path claims. Fall through to the canonical URL rather
                    # than pooling it forever under the expected checksum.
                    last = "by-hash content did not match its own checksum"
                else:
                    return Fetch.OK, content, None
            else:
                last = f"http {resp.status_code}"
                # 403 and friends are a real answer, not a blip — stop asking.
                if resp.status_code not in (404, 429) and resp.status_code < 500:
                    return Fetch.FAILED, None, last

        # Ask the canonical URL what's going on before burning any backoff.
        outcome, content, reason = _fetch_canonical(mf, client, last)
        if outcome is not Fetch.FAILED:
            return outcome, content, reason
        if attempt < _BY_HASH_RETRIES - 1:
            # `stop_event.wait(delay)` rather than `time.sleep(delay)`: this
            # runs on one of `max_connections` executor threads and the total
            # backoff is ~62 s per file, so an uninterruptible sleep held the
            # sync TUI's `on_unmount` (which shuts the executor down with
            # wait=True) for minutes after the user pressed quit.
            if stop_event is not None:
                if stop_event.wait(delay):
                    return Fetch.FAILED, None, "stopped"
            else:
                time.sleep(delay)
            delay *= 2

    return Fetch.FAILED, None, f"by-hash {last}, canonical checksum mismatch"


def _fetch_canonical(mf: MetadataFile, client, by_hash_status: str) -> FetchResult:
    """Canonical-URL fetch used to resolve a by-hash miss.  FAILED here means
    'inconclusive, worth retrying', not 'give up'."""
    try:
        resp = client.get(mf.fallback_url)
    except Exception as e:
        return Fetch.FAILED, None, f"by-hash {by_hash_status}, canonical {type(e).__name__}"
    if resp.status_code == 404:
        return Fetch.NOT_FOUND, None, None       # absent upstream — settled, no retries
    if not resp.is_success:
        return Fetch.FAILED, None, f"by-hash {by_hash_status}, canonical http {resp.status_code}"
    content = resp.content
    if mf.expected_sha256 and hashlib.sha256(content).hexdigest() != mf.expected_sha256:
        # Our Release snapshot and this file disagree — retry, then re-snapshot.
        return Fetch.FAILED, None, f"by-hash {by_hash_status}, canonical checksum mismatch"
    return Fetch.OK, content, None


def _meta_filename(repo_path: str) -> str:
    name = posixpath.basename(repo_path)
    return name or "index"


def _fetch_or_skip(mf: MetadataFile, pooled: "set[str]", stop_event=None) -> FetchResult:
    """Skip the network when the verified blob is already pooled.

    Shared suite-level files (InRelease, Contents-*.gz) have the same checksum
    for every component mirror of a source URL — the first component pools the
    blob, the rest see ALREADY_DONE.

    Takes the already-known pooled set rather than querying per file. Each
    `pool.contains()` acquires the pool's read lock, and these run one per
    metadata file across all 32 executor threads at once: profiled at **40% of
    every worker's samples blocked in `pool._read`**, with the top stack
    `pool.contains <- _fetch_or_skip`. One batched query per mirror replaces
    hundreds of individually-locked point lookups.
    """
    if mf.expected_sha256 is not None and mf.expected_sha256 in pooled:
        return Fetch.ALREADY_DONE, None, None
    return _fetch_metadata_content(mf, stop_event)


def _fetch_and_pool(mf: MetadataFile, pool: Pool, stop_event=None) -> tuple[str | None, str | None]:
    """Fetch an unverified metadata file (release file, .asc, …) and pool it.

    Returns (error, checksum): (None, sha) on success, (None, None) on upstream
    404, (message, None) on failure.
    """
    outcome, content, reason = _fetch_metadata_content(mf, stop_event)
    if outcome is Fetch.FAILED:
        return f"{mf.repo_path}: fetch failed ({reason})", None
    if content is None:
        return None, None  # NOT_FOUND — absent upstream
    checksum = hashlib.sha256(content).hexdigest()
    try:
        pool.add(content, checksum, "sha256", _meta_filename(mf.repo_path), len(content))
    except OSError as e:
        return f"{mf.repo_path}: pool write failed ({e})", None
    return None, checksum


class PermanentFetchError(Exception):
    """An answer, not a blip — retrying it cannot help.

    A 404 (package pulled from the archive mid-sync) or a 403 used to be
    retried three times back to back, which is three round trips spent
    confirming something the first response already settled.
    """


def _partial_path(pool: Pool, pkg: PackageInfo) -> Path:
    """A resume file for ONE download of `pkg`: unique to it, and reused by
    that download's own retries (`_do_one` makes it once and hands it to
    every attempt, since a retry may land on another executor thread).

    Never derived from the URL alone. `Architecture: all` packages are in
    every arch's index and the TUI syncs all mirrors at once, so two mirrors
    routinely download one URL at the same moment. With a URL-keyed name the
    second `open("wb")` replaced the first's file, the first finished and
    linked the second's *half-written* file into the pool under the correct
    checksum, and a stop or a dropped connection on the second left it
    truncated there for good — a pooled checksum is never fetched again.
    Abandoned partials are swept by the pool's 24 h tmp reaper.
    """
    key = hashlib.sha256(pkg.url.encode()).hexdigest()[:16]
    return pool._tmp_dir / f"{os.getpid()}-part-{key}-{uuid.uuid4().hex[:12]}-{pkg.filename}"


def _download_sync(
    pkg: PackageInfo,
    pool: Pool,
    chunk_callback: ChunkCallback | None,
    start_callback: Callable[[], None] | None,
    stop_event: threading.Event | None = None,
    resume: bool = False,
    *,
    partial: Path | None = None,
) -> DownloadResult:
    """Stream pkg from network into the pool. Runs in executor thread.

    With `resume`, an interrupted attempt's bytes are kept and continued with a
    `Range:` request instead of being thrown away. A 4 GB package that drops at
    99% used to cost the full download again, three times, on a link that had
    already proved flaky.

    Resuming is safe because the checksum is verified over the *whole* file
    before anything is pooled: if the server ignores the Range (answering 200),
    we start over; if it serves the wrong bytes, the digest catches it and the
    partial is discarded. No trust is placed in the range machinery itself.

    `partial` is the resume file `_do_one` made for this package; a call
    without one (a single attempt) gets a fresh one of its own.
    """
    if start_callback:
        start_callback()
    client = _thread_client()
    h = hashlib.new(pkg.checksum_type)
    tmp = partial if partial is not None else _partial_path(pool, pkg)

    offset = 0
    if resume and tmp.exists():
        try:
            with open(tmp, "rb") as f:
                for block in iter(lambda: f.read(1 << 20), b""):
                    h.update(block)
                    offset += len(block)
        except OSError:
            offset = 0
            h = hashlib.new(pkg.checksum_type)
    if offset == 0:
        tmp.unlink(missing_ok=True)

    bytes_written = offset
    headers = {"Range": f"bytes={offset}-"} if offset else None
    try:
        with request_scope(stop_event), client.stream("GET", pkg.url, headers=headers) as resp:
            if resp.status_code in (403, 404, 410):
                raise PermanentFetchError(f"http {resp.status_code}")
            if offset and resp.status_code != 206:
                # Range ignored (or unsupported) — start clean rather than
                # appending to bytes the server is about to resend.
                h = hashlib.new(pkg.checksum_type)
                bytes_written = 0
                offset = 0
            resp.raise_for_status()
            with open(tmp, "ab" if offset else "wb") as f:
                for chunk in resp.iter_bytes(65536):
                    if stop_event and stop_event.is_set():
                        raise InterruptedError("sync stopped")
                    f.write(chunk)
                    h.update(chunk)
                    bytes_written += len(chunk)
                    if chunk_callback:
                        chunk_callback(len(chunk))
        checksum = h.hexdigest()
        if pkg.checksum_known and checksum != pkg.checksum:
            tmp.unlink(missing_ok=True)   # a resumed partial may be the culprit
            return DownloadResult(status=Status.FAILED, path=tmp, error="checksum mismatch")
        pool_path = pool.add(tmp, checksum, pkg.checksum_type, pkg.filename, bytes_written,
                             source_url=pkg.url if not pkg.checksum_known else "")
        return DownloadResult(
            status=Status.DOWNLOADED, path=pool_path,
            bytes_transferred=bytes_written, checksum=checksum,
        )
    except (PermanentFetchError, InterruptedError):
        tmp.unlink(missing_ok=True)
        raise
    except Exception:
        # Deliberately NOT unlinked: whatever landed is what the next attempt
        # resumes from. Only a clean finish, a permanent answer or a bad digest
        # discards it; anything left behind is swept by the tmp reaper.
        raise


def lookup_packages(
    packages: list[PackageInfo], pool: Pool, mirror_name: str,
) -> tuple[set[str], dict[str, str], dict[tuple[str, str], str]]:
    """Batch membership checks, falling back to old refs only where needed.

    URL identities survive interrupted syncs and take precedence over legacy
    path memory. Once those identities cover a unit, loading and checking its
    entire previous ref map (metadata included) is redundant.
    """
    pooled = pool.batch_contains([p.checksum for p in packages if p.checksum_known and p.checksum])
    unknown = [p for p in packages if not p.checksum_known]
    sources = pool.download_checksums([(p.url, p.checksum_type) for p in unknown]) if unknown else {}
    fallback_paths = {p.repo_path for p in unknown if (p.url, p.checksum_type.lower()) not in sources}
    legacy = {}
    if fallback_paths:
        refs = {path: cs for path, cs in pool.ref_map(mirror_name).items() if path in fallback_paths}
        have = pool.batch_contains(list(refs.values()))
        legacy = {path: cs for path, cs in refs.items() if cs in have}
    return pooled, legacy, sources


async def _verify_metadata_batch(files, pooled, executor, stop_event, max_connections):
    """Skip cached entries without executor jobs; bound network work and hash
    pre-fetched metadata in bounded chunks instead of one future per JSON.
    Large prefetched APT indexes keep their parallel hash checks."""
    results: list[FetchResult | BaseException] = [(Fetch.ALREADY_DONE, None, None)] * len(files)
    work = collections.deque()
    inline, inline_bytes = [], 0
    for index, mf in enumerate(files):
        if mf.expected_sha256 not in pooled:
            if mf.content is None:
                work.append([(index, mf)])
            else:
                if inline and (len(inline) >= 1000 or inline_bytes + len(mf.content) > (8 << 20)):
                    work.append(inline)
                    inline, inline_bytes = [], 0
                inline.append((index, mf))
                inline_bytes += len(mf.content)
    if inline:
        work.append(inline)

    def check(index, mf):
        try:
            results[index] = _fetch_or_skip(mf, pooled, stop_event)
        except Exception as error:
            results[index] = error

    def check_group(group):
        for index, mf in group:
            check(index, mf)

    loop = asyncio.get_running_loop()
    async def worker():
        while work:
            group = work.popleft()
            await loop.run_in_executor(executor, check_group, group)

    await asyncio.gather(*[worker() for _ in range(min(max(1, max_connections), len(work)))])
    return results


def _store_verified_metadata(files, results, pool, stop_event, phase_callback):
    """Keep small sequential HDD writes together, off the event loop."""
    seen = set()
    for index, (mf, result) in enumerate(zip(files, results)):
        if stop_event is not None and stop_event.is_set():
            return False
        outcome, content, _reason = result
        if outcome is Fetch.OK and content is not None and mf.expected_sha256 not in seen:
            pool.add(content, mf.expected_sha256, "sha256", _meta_filename(mf.repo_path), len(content))
            seen.add(mf.expected_sha256)
        if phase_callback and (index + 1) % 1000 == 0:
            phase_callback(f"Storing verified metadata: {index + 1:,}/{len(files):,} files...")
    return True


async def download_mirror(
    packages: list[PackageInfo],
    metadata: list[MetadataFile],
    pool: Pool,
    mirror_name: str,
    max_connections: int,
    source: str = "",
    progress: ProgressCallback | None = None,
    phase_callback: PhaseCallback | None = None,
    chunk_callback: ChunkCallback | None = None,
    start_callback: Callable[[], None] | None = None,
    dry_run: bool = False,
    refetch_metadata: Callable[[], list[MetadataFile]] | None = None,
    executor: concurrent.futures.Executor | None = None,
    sem: asyncio.Semaphore | None = None,
    stop_event: threading.Event | None = None,
    file_progress: Callable[[PackageInfo, DownloadResult], None] | None = None,
    downloads: "DownloadCoordinator | None" = None,
    finish_callback: Callable[[], None] | None = None,
) -> MirrorSummary:
    """Sync one mirror into the pool: blobs + a desired-state refs snapshot.

    No trees are written. Refs are reconciled (one atomic transaction) only
    when the metadata phase commits cleanly and the sync wasn't stopped —
    otherwise the previous consistent snapshot stays in force.
    """
    summary = MirrorSummary(mirror=mirror_name, total=len(packages))
    loop = asyncio.get_running_loop()
    if downloads is None:
        from mmw.core.download_coordinator import DownloadCoordinator
        downloads = DownloadCoordinator()

    def _progress(package: PackageInfo, result: DownloadResult) -> None:
        if progress:
            progress(package.name, result)
        if file_progress:
            file_progress(package, result)

    def record_time(phase, started):
        summary.timings[phase] = summary.timings.get(phase, 0.0) + time.perf_counter() - started

    _owns_executor = executor is None
    if _owns_executor:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_connections)

    # Held for this whole mirror so a concurrent `mmw prune` refuses instead of
    # deleting the blobs being downloaded right now — their refs are not
    # committed until the reconcile at the end, so until then every one of them
    # looks exactly like an orphan.
    #
    # **On the executor, never here.** This coroutine runs on the event loop
    # (in the TUI, `sync.py:_sync_mirror` is an async worker), and the lock
    # does `mkdir` + `open` + `flock` — three blocking filesystem calls. Taking
    # them inline froze a real 45-mirror sync solid: the profiler put the
    # MainThread at 21.8% of samples inside `shared_pool_lock`, under
    # `download_mirror <- sync_mirror <- _sync_mirror`. The lock itself is now
    # process-wide and refcounted, so only the first mirror pays for it at all.
    await loop.run_in_executor(executor, acquire_sync_lock, pool.pool_dir.parent)

    # Desired-state entries accumulated for the final reconcile.
    pkg_entries: list[tuple[str, str, str]] = []
    meta_entries: list[tuple[str, str, str]] = []

    try:
        # ── Phase 1: classify — batch SQL, no per-package I/O ─────────────────
        if phase_callback:
            phase_callback(f"Checking pool for {len(packages):,} files...")
        started = time.perf_counter()
        pooled_set, legacy_known, source_known = await loop.run_in_executor(
            executor, lookup_packages, packages, pool, mirror_name)
        record_time("pool lookup", started)
        started = time.perf_counter()

        to_download: list[PackageInfo] = []
        emitted = 0

        for pkg in packages:
            # Yield per iteration, not per pooled package: on a fresh sync every
            # package takes the to_download branch, so a counter bumped only in
            # the POOLED branch never fires and the whole classify pass runs
            # without releasing the event loop.
            emitted += 1
            if emitted % _PROGRESS_YIELD == 0:
                await asyncio.sleep(0)

            if not is_safe_repo_path(pkg.repo_path):
                summary.failed += 1
                summary.errors.append(f"{pkg.name}: unsafe path in upstream metadata: {pkg.repo_path!r}")
                _progress(pkg, DownloadResult(Status.FAILED, Path(pkg.filename), error="unsafe path"))
                continue

            if pkg.checksum_known and pkg.checksum:
                # Desired state includes packages we have AND ones we still
                # need (failed downloads show up as refs-without-packages).
                pkg_entries.append((pkg.checksum, pkg.repo_path, KIND_PKG))
                if pkg.checksum in pooled_set:
                    summary.pooled += 1
                    _progress(pkg, DownloadResult(Status.POOLED, Path(pkg.repo_path)))
                else:
                    to_download.append(pkg)
            elif not pkg.checksum_known and (
                    (pkg.url, pkg.checksum_type.lower()) in source_known or pkg.repo_path in legacy_known):
                checksum = source_known.get((pkg.url, pkg.checksum_type.lower())) or legacy_known[pkg.repo_path]
                pkg_entries.append((checksum, pkg.repo_path, KIND_PKG))
                if (pkg.url, pkg.checksum_type.lower()) in source_known:
                    summary.pooled += 1
                    status = Status.POOLED
                else:
                    summary.skipped += 1
                    status = Status.SKIPPED
                _progress(pkg, DownloadResult(status, Path(pkg.repo_path), checksum=checksum))
            else:
                to_download.append(pkg)

        record_time("package classification", started)
        # ── Phase 2: package downloads → pool ─────────────────────────────────
        if phase_callback and not (stop_event and stop_event.is_set()):
            phase_callback(f"Downloading {len(to_download):,} files...")

        if not dry_run and to_download and not (stop_event and stop_event.is_set()):
            # Refuse up front when the disk plainly cannot hold this.
            #
            # Filling the disk mid-sync used to surface as N thousand
            # per-package OSErrors plus (until the writer's errors were given a
            # reader) silently dropped database rows — a state indistinguishable
            # from an upstream problem, on a mirror that is now half-populated.
            # The sizes are already in hand from classification, so the check is
            # free; it is a *guard*, not a guarantee, since other writers share
            # the filesystem, hence the margin.
            want = sum(p.size for p in to_download if p.size)
            if want:
                try:
                    # On the executor: `statvfs` is microseconds on a local
                    # disk and can block for seconds on a stale NFS mount —
                    # and an NFS-mounted `mirror_dir` is the normal case for
                    # this tool, not an exotic one. This coroutine runs on the
                    # event loop in the TUI.
                    free = await loop.run_in_executor(
                        executor, lambda: shutil.disk_usage(pool.pool_dir).free)
                except OSError:
                    free = None
                if free is not None and want + _DISK_MARGIN > free:
                    summary.metadata_ok = False
                    summary.metadata_errors.append(
                        f"not enough free space: need ~{want:,} bytes plus a "
                        f"{_DISK_MARGIN:,} byte margin, {free:,} free at "
                        f"{pool.pool_dir} — nothing was downloaded"
                    )
                    to_download = []
            _sem = sem if sem is not None else asyncio.Semaphore(max_connections)

            async def _do_one(pkg: PackageInfo) -> None:
                started = False

                def _started():
                    nonlocal started
                    if not started:
                        started = True
                        if start_callback:
                            start_callback()

                def _finished():
                    if started and finish_callback:
                        finish_callback()

                async def _fetch_candidate(url, cached_checksum):
                    candidate = replace(pkg, url=url)
                    async with _sem:
                        if stop_event and stop_event.is_set():
                            return None
                        checksum = pkg.checksum if pkg.checksum_known else cached_checksum
                        if not checksum and not pkg.checksum_known:
                            key = (url, pkg.checksum_type.lower())
                            sources = await loop.run_in_executor(executor, pool.download_checksums, [key])
                            checksum = sources.get(key, "")
                        if checksum and await loop.run_in_executor(executor, pool.contains, checksum):
                            return DownloadResult(Status.POOLED, Path(pkg.repo_path), checksum=checksum)
                        return await _fetch(candidate)

                async def _fetch(candidate):
                    result = DownloadResult(Status.FAILED, Path(pkg.filename), error="not attempted")
                    delay = _PKG_BACKOFF
                    partial = _partial_path(pool, candidate)  # private to this URL and its retries
                    for attempt in range(_PKG_RETRIES):
                        if stop_event and stop_event.is_set():
                            return None  # teardown — not a download failure
                        try:
                            fn = functools.partial(
                                _download_sync, candidate, pool,
                                chunk_callback, _started if attempt == 0 else None,
                                stop_event, attempt > 0,     # resume after the first try
                                partial=partial,
                            )
                            result = await loop.run_in_executor(executor, fn)
                        except InterruptedError:
                            return None  # stopped mid-stream — not a download failure
                        except PermanentFetchError as exc:
                            # A real answer (404/403/410): the package was
                            # pulled from the archive mid-sync, or we are not
                            # allowed to have it. Asking twice more just spends
                            # round trips confirming it.
                            result = DownloadResult(
                                Status.FAILED, Path(pkg.filename), error=str(exc)
                            )
                            break
                        except Exception as exc:
                            result = DownloadResult(
                                Status.FAILED, Path(pkg.filename), error=str(exc) or type(exc).__name__
                            )
                        if result.status != Status.FAILED:
                            break
                        if attempt < _PKG_RETRIES - 1:
                            # Back off before retrying. Three immediate attempts
                            # put all of them inside the same overloaded second,
                            # which is the moment least likely to succeed.
                            # Slept in slices so a stop is noticed promptly
                            # without parking an executor thread on an Event.
                            slept = 0.0
                            while slept < delay:
                                if stop_event and stop_event.is_set():
                                    return None
                                step = min(_STOP_POLL, delay - slept)
                                await asyncio.sleep(step)
                                slept += step
                            delay *= 2
                    return result

                result = await downloads.run(pool, pkg, _fetch_candidate, stop_event, _finished)
                if result is None:
                    summary.stopped = True
                    return

                match result.status:
                    case Status.DOWNLOADED:
                        summary.downloaded += 1
                        summary.bytes_transferred += result.bytes_transferred
                    case Status.POOLED:
                        summary.pooled += 1
                    case Status.FAILED:
                        summary.failed += 1
                        summary.errors.append(f"{pkg.name}: {result.error}")

                if result.status in (Status.DOWNLOADED, Status.POOLED) and not pkg.checksum_known and result.checksum:
                    # Each waiter needs its own ref, even when another unit
                    # downloaded the shared URL and computed its checksum.
                    pkg_entries.append((result.checksum, pkg.repo_path, KIND_PKG))

                _progress(pkg, result)

            # A fixed pool of consumers, NOT one task per package.
            #
            # `asyncio.gather(*[_do_one(p) for p in to_download])` created one
            # Task per package up front — 65,000 for a single ubuntu-ports
            # component — and every one of them queued on the shared semaphore.
            # Profiled: **52.9% of the event-loop thread** inside
            # `Semaphore._wake_up_next`, waking the next of tens of thousands of
            # waiters on every completion, and ~1.5 GB RSS in Task objects. The
            # semaphore still bounds *concurrency*; it never bounded allocation.
            #
            # The semaphore is deliberately kept: `SyncView` passes one shared
            # instance so `max_connections` bounds downloads across *all*
            # mirrors at once, not per mirror.
            pending = collections.deque(to_download)

            async def _consume() -> None:
                while pending:
                    if stop_event and stop_event.is_set():
                        summary.stopped = True
                        return
                    pkg = pending.popleft()   # single-threaded loop: no race
                    try:
                        await _do_one(pkg)
                    except Exception as exc:  # a progress callback must not
                        summary.failed += 1   # take the whole consumer down
                        summary.errors.append(f"{pkg.name}: {exc}")

            await asyncio.gather(
                *[_consume() for _ in range(min(max_connections, len(to_download)))]
            )

        # ── Phase 3: metadata blobs ────────────────────────────────────────────
        # Verified files are fetched into memory and ALL checked before any
        # entry is recorded; nothing becomes visible until the refs flip, so a
        # mid-update CDN mismatch simply leaves the previous snapshot in force.
        if phase_callback:
            phase_callback("Syncing metadata...")
        if summary.stopped or (stop_event and stop_event.is_set()):
            if not dry_run:
                summary.stopped = True
                summary.metadata_ok = False
                summary.metadata_errors.append("sync stopped — index left unchanged")
        elif not dry_run and not metadata and pool.has_index_refs(mirror_name):
            # Phase 4 reconciles `pkg_entries + meta_entries` as the mirror's
            # complete desired state, and `reconcile_refs` DELETEs the old set
            # first — so committing an empty metadata half erases every index
            # and release ref this mirror had, while `summary.ok` stays true,
            # headless exits 0 and the TUI card goes green. The next
            # `prune = true` publish then deletes `dists/` from a served tree.
            #
            # An empty list is reachable without an attacker: the provider
            # reports per-file failures by omission, so a CDN 503 on both
            # InRelease and Release, or a transport blip, yields nothing at
            # all. (`providers/apt.py` now distinguishes those from a real
            # 404 — this is the backstop for anything that still slips
            # through.) Only the destructive case aborts: a first sync with no
            # metadata has no index to lose and is left alone.
            summary.metadata_ok = False
            summary.metadata_errors.append(
                "upstream returned no metadata files at all — index left unchanged "
                "(committing this would have deleted the mirror's existing index)"
            )
        elif not dry_run and metadata:
            unsafe = [mf.repo_path for mf in metadata if not is_safe_repo_path(mf.repo_path)]
            if unsafe:
                summary.metadata_ok = False
                summary.metadata_errors.append(
                    "unsafe metadata path(s) from upstream, index not updated: "
                    + ", ".join(repr(p) for p in unsafe[:3])
                    + ("…" if len(unsafe) > 3 else "")
                )
                metadata = []

        # Up to two attempts: a long package phase can outlive the Release
        # snapshot we started from (Ubuntu republishes -updates/-security
        # several times a day, -proposed constantly).  When that happens the
        # by-hash entries for our hashes are pruned and the canonical files no
        # longer match, so the whole index would abort even though nothing is
        # wrong.  Re-fetch the index and verify against the *current* snapshot
        # instead of failing.
        for _attempt in range(2):
            if dry_run or not metadata or not summary.metadata_ok or summary.stopped:
                break

            meta_entries.clear()
            # `is_release` is declared by the provider rather than inferred
            # from which other fields happen to be set: the old rule ("content
            # is set and expected_sha256 is not") filed a flat repository's
            # release files as KIND_META while its provider only HEADed them,
            # losing the release-last publish ordering that keeps a
            # live-served tree consistent.
            def _is_release(mf: MetadataFile) -> bool:
                # The flag, and only the flag. There used to be a fallback
                # inference here — "content is set and expected_sha256 is not"
                # — kept for providers written before `is_release` existed.
                # Both providers set it now, and the inference is not merely
                # redundant: the moment a provider hands over an index whose
                # bytes it already fetched (which is exactly what `fetch_all`
                # does to avoid downloading the largest index twice), a flat
                # repository's `Packages.xz` matched it and was filed as a
                # trust root — published LAST, alongside InRelease, so a client
                # mid-`apt update` could see a release file whose indexes had
                # not landed. Inferring a file's role from which other fields
                # happen to be populated is what caused this twice; naming it
                # is the fix both times.
                return bool(getattr(mf, "is_release", False))

            release_files = [mf for mf in metadata if _is_release(mf)]
            verified_files = [mf for mf in metadata
                              if mf.expected_sha256 is not None and not _is_release(mf)]
            other_files    = [mf for mf in metadata
                              if mf.expected_sha256 is None and not _is_release(mf)]

            # Fetch all verified files — pool-membership check first, network
            # only if absent. One batched lookup, off the event loop, instead of
            # a locked point lookup per file on every worker thread.
            wanted = [mf.expected_sha256 for mf in verified_files if mf.expected_sha256]
            started = time.perf_counter()
            already_pooled = await loop.run_in_executor(
                executor, pool.batch_contains, wanted
            ) if wanted else set()
            record_time("metadata lookup", started)
            if phase_callback:
                phase_callback(f"Verifying metadata: {len(verified_files):,} files...")
            started = time.perf_counter()
            fetch_results = await _verify_metadata_batch(
                verified_files, already_pooled, executor, stop_event, max_connections)
            record_time("metadata verification", started)

            failed_paths = [
                f"{mf.repo_path} ({type(r).__name__ if isinstance(r, BaseException) else r[2]})"
                for mf, r in zip(verified_files, fetch_results)
                if isinstance(r, BaseException) or r[0] is Fetch.FAILED
            ]
            summary.metadata_missing = sum(
                1 for r in fetch_results
                if not isinstance(r, BaseException) and r[0] is Fetch.NOT_FOUND
            )

            if failed_paths:
                # CDN mid-update or persistent failure — abort the whole index
                # update; refs stay on the previous consistent snapshot.
                summary.metadata_ok = False
                summary.metadata_errors.append(
                    f"index not updated — {len(failed_paths)} file(s) failed verification: "
                    + ", ".join(failed_paths[:3])
                    + ("…" if len(failed_paths) > 3 else "")
                )
            else:
                if phase_callback:
                    phase_callback(f"Storing verified metadata: {len(verified_files):,} files...")
                started = time.perf_counter()
                # Pool freshly fetched verified blobs; ALREADY_DONE blobs are
                # referenced as-is. NOT_FOUND files get no entry.
                stored = await loop.run_in_executor(
                    executor, _store_verified_metadata, verified_files, fetch_results,
                    pool, stop_event, phase_callback)
                record_time("metadata writes", started)
                if not stored:
                    summary.stopped = True
                    summary.metadata_ok = False
                    summary.metadata_errors.append("sync stopped — index left unchanged")
                    break
                for mf, r in zip(verified_files, fetch_results):
                    outcome, _content, _reason = r
                    if outcome in (Fetch.OK, Fetch.ALREADY_DONE):
                        meta_entries.append((mf.expected_sha256, mf.repo_path, KIND_META))

                started = time.perf_counter()
                # Other files (Release.gpg, .asc, dist-upgrader, …) in parallel.
                # A failure here is reported but doesn't abort the index — same
                # availability the upstream offers without that file.
                other_results = await asyncio.gather(
                    *[loop.run_in_executor(executor, _fetch_and_pool, mf, pool, stop_event) for mf in other_files],
                    return_exceptions=True,
                )
                for mf, r in zip(other_files, other_results):
                    if isinstance(r, BaseException):
                        summary.metadata_errors.append(str(r) or type(r).__name__)
                        continue
                    err, checksum = r
                    if err:
                        summary.metadata_errors.append(err)
                    elif checksum:
                        meta_entries.append((checksum, mf.repo_path, KIND_META))

                # Release files (the trust roots) — entries marked so publish
                # places them last at the destination.
                for mf in release_files:
                    err, checksum = await loop.run_in_executor(executor, _fetch_and_pool, mf, pool, stop_event)
                    if err:
                        summary.metadata_ok = False
                        summary.metadata_errors.append(err)
                    elif checksum:
                        meta_entries.append((checksum, mf.repo_path, KIND_RELEASE))
                record_time("other metadata", started)

            if summary.metadata_ok or refetch_metadata is None or _attempt == 1:
                break
            if summary.stopped or (stop_event and stop_event.is_set()):
                break

            # Upstream moved under us — take a fresh snapshot and verify against it.
            summary.metadata_ok = True
            summary.metadata_errors.clear()
            summary.metadata_resnapshotted = True
            if phase_callback:
                phase_callback("Index changed upstream — re-fetching...")
            try:
                metadata = await loop.run_in_executor(executor, refetch_metadata)
            except Exception as e:
                summary.metadata_ok = False
                summary.metadata_errors.append(f"index re-fetch failed: {type(e).__name__}: {e}")
                break
            unsafe = [mf.repo_path for mf in metadata if not is_safe_repo_path(mf.repo_path)]
            if unsafe:
                summary.metadata_ok = False
                summary.metadata_errors.append(
                    "unsafe metadata path(s) from upstream, index not updated: "
                    + ", ".join(repr(pth) for pth in unsafe[:3])
                    + ("…" if len(unsafe) > 3 else "")
                )
                break

        if stop_event is not None and stop_event.is_set() and not dry_run:
            summary.stopped = True
            summary.metadata_ok = False
        # ── Phase 4: reconcile — the atomic commit ────────────────────────────
        if not dry_run and summary.metadata_ok and not summary.stopped:
            reconcile_started = time.perf_counter()
            if phase_callback:
                phase_callback("Committing pool references...")
            entries = pkg_entries + meta_entries
            # Prefer the pool's writer thread: this is a single ~2.3 s
            # transaction, and running it on an executor thread parks one of the
            # 32 threads that also stream downloads. Awaiting a future costs no
            # thread at all. Falls back to the executor when deferred writes are
            # switched off (MMW_DEFER_WRITES=0).
            fut = loop.create_future()

            def _done(error, _loop=loop, _fut=fut):
                _loop.call_soon_threadsafe(
                    lambda: None if _fut.done() else (
                        _fut.set_exception(error) if error else _fut.set_result(None)))

            if pool.submit_reconcile(mirror_name, entries, source, _done):
                await fut
            else:
                await loop.run_in_executor(
                    executor, pool.reconcile_refs, mirror_name, entries, source
                )
            summary.reconcile_seconds = time.perf_counter() - reconcile_started
            summary.timings["reference commit"] = summary.reconcile_seconds
            if phase_callback:
                phase_callback(f"Pool references committed in {summary.reconcile_seconds:.2f}s")

    finally:
        if _owns_executor:
            executor.shutdown(wait=False)
        # Drained here rather than ignored: a dropped package row is the one
        # failure mode that leaves the pool *looking* healthy while publish
        # silently ships less than the index promises.
        try:
            summary.pool_write_errors.extend(pool.take_write_errors())
        except Exception:
            pass
        release_sync_lock()

    return summary
