import asyncio
import collections
import concurrent.futures
import functools
import hashlib
import os
import posixpath
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from mmw.core.keys import KeySyncResult

import httpx

from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool
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

    @property
    def ok(self) -> bool:
        return not self.failed and self.metadata_ok and not self.stopped


ProgressCallback = Callable[[str, DownloadResult], None]
PhaseCallback = Callable[[str], None]
ChunkCallback = Callable[[int], None]

# One sync httpx client per thread — sync Client is not thread-safe to share.
_thread_local = threading.local()


def _thread_client() -> httpx.Client:
    if not hasattr(_thread_local, "client"):
        _thread_local.client = httpx.Client(follow_redirects=True, timeout=60)
    return _thread_local.client


def fetch_packages_threaded(provider, mirror_config, status_callback=None) -> list[PackageInfo]:
    """Submit to an executor — calls provider.fetch_packages with the thread-local client."""
    return provider.fetch_packages(mirror_config, _thread_client(), status_callback=status_callback)


def fetch_metadata_threaded(provider, mirror_config) -> list[MetadataFile]:
    """Submit to an executor — calls provider.fetch_metadata with the thread-local client."""
    return provider.fetch_metadata(mirror_config, _thread_client())


def fetch_all_threaded(provider, mirror_config, status_callback=None) -> tuple[list[PackageInfo], list[MetadataFile]]:
    """Submit to an executor — packages + metadata from one consistent snapshot."""
    return provider.fetch_all(mirror_config, _thread_client(), status_callback=status_callback)


def sync_keys_threaded(distro_def, mirror_dir: Path) -> "KeySyncResult":
    """Submit to an executor — calls keys.sync_keys with the thread-local client."""
    from mmw.core.keys import sync_keys
    return sync_keys(distro_def, mirror_dir, _thread_client())


def sync_custom_key_threaded(mirror_name: str, key_url: str, mirror_dir: Path) -> "KeySyncResult":
    """Submit to an executor — fetches a single GPG key URL with the thread-local client."""
    from mmw.core.keys import sync_custom_key
    return sync_custom_key(mirror_name, key_url, mirror_dir, _thread_client())


_BY_HASH_RETRIES = 5
_BY_HASH_BACKOFF  = 2  # seconds; doubles each retry: 2 4 8 16 32 → ~62 s total

# Yield to the event loop this often while emitting bulk POOLED progress so a
# huge mirror (100k+ pooled packages) doesn't freeze the TUI for seconds.
_PROGRESS_YIELD = 2_000


def _fetch_metadata_content(mf: MetadataFile) -> FetchResult:
    """Fetch and verify a metadata file's bytes. Does not touch the pool."""
    if mf.content is not None:
        content = mf.content
        if mf.expected_sha256 and hashlib.sha256(content).hexdigest() != mf.expected_sha256:
            return Fetch.FAILED, None, "checksum mismatch on pre-fetched content"
        return Fetch.OK, content, None

    if mf.fallback_url is not None:
        return _fetch_by_hash(mf)

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


def _fetch_by_hash(mf: MetadataFile) -> FetchResult:
    """Fetch a verified index file, preferring the by-hash URL.

    By-hash content IS the hash, so a 200 needs no sha256 check.  A by-hash 404
    has two very different causes and the canonical URL tells them apart
    immediately, so it is consulted *before* spending any backoff:

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
        try:
            resp = client.get(mf.url)
        except Exception as e:
            last = type(e).__name__
        else:
            if resp.is_success:
                return Fetch.OK, resp.content, None  # content IS the hash
            last = f"http {resp.status_code}"
            # 403 and friends are a real answer, not a blip — stop asking.
            if resp.status_code not in (404, 429) and resp.status_code < 500:
                return Fetch.FAILED, None, last

        # Ask the canonical URL what's going on before burning any backoff.
        outcome, content, reason = _fetch_canonical(mf, client, last)
        if outcome is not Fetch.FAILED:
            return outcome, content, reason
        if attempt < _BY_HASH_RETRIES - 1:
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


def _fetch_or_skip(mf: MetadataFile, pooled: "set[str]") -> FetchResult:
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
    return _fetch_metadata_content(mf)


def _fetch_and_pool(mf: MetadataFile, pool: Pool) -> tuple[str | None, str | None]:
    """Fetch an unverified metadata file (release file, .asc, …) and pool it.

    Returns (error, checksum): (None, sha) on success, (None, None) on upstream
    404, (message, None) on failure.
    """
    outcome, content, reason = _fetch_metadata_content(mf)
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


def _download_sync(
    pkg: PackageInfo,
    pool: Pool,
    chunk_callback: ChunkCallback | None,
    start_callback: Callable[[], None] | None,
    stop_event: threading.Event | None = None,
) -> DownloadResult:
    """Stream pkg from network into the pool. Runs in executor thread."""
    if start_callback:
        start_callback()
    client = _thread_client()
    h = hashlib.new(pkg.checksum_type)
    bytes_written = 0
    tmp = pool._tmp_dir / f"{os.getpid()}-{threading.get_ident()}-{pkg.filename}"
    try:
        with client.stream("GET", pkg.url) as resp:
            resp.raise_for_status()
            with open(tmp, "wb") as f:
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
            tmp.unlink(missing_ok=True)
            return DownloadResult(status=Status.FAILED, path=tmp, error="checksum mismatch")
        pool_path = pool.add(tmp, checksum, pkg.checksum_type, pkg.filename, bytes_written)
        return DownloadResult(
            status=Status.DOWNLOADED, path=pool_path,
            bytes_transferred=bytes_written, checksum=checksum,
        )
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        raise exc


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
) -> MirrorSummary:
    """Sync one mirror into the pool: blobs + a desired-state refs snapshot.

    No trees are written. Refs are reconciled (one atomic transaction) only
    when the metadata phase commits cleanly and the sync wasn't stopped —
    otherwise the previous consistent snapshot stays in force.
    """
    summary = MirrorSummary(mirror=mirror_name, total=len(packages))
    loop = asyncio.get_running_loop()

    _owns_executor = executor is None
    if _owns_executor:
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_connections)

    # Desired-state entries accumulated for the final reconcile.
    pkg_entries: list[tuple[str, str, str]] = []
    meta_entries: list[tuple[str, str, str]] = []

    try:
        # ── Phase 1: classify — batch SQL, no per-package I/O ─────────────────
        if phase_callback:
            phase_callback(f"Checking pool for {len(packages):,} packages...")
        known_checksums = [p.checksum for p in packages if p.checksum_known and p.checksum]
        pooled_set: set[str] = await loop.run_in_executor(executor, pool.batch_contains, known_checksums)

        # Providers with no upstream checksums: previous refs map paths to the
        # checksums computed when those paths were last downloaded.
        legacy_known: dict[str, str] = {}
        if any(not p.checksum_known for p in packages):
            refmap = await loop.run_in_executor(executor, pool.ref_map, mirror_name)
            have = await loop.run_in_executor(executor, pool.batch_contains, list(refmap.values()))
            legacy_known = {path: cs for path, cs in refmap.items() if cs in have}

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
                if progress:
                    progress(pkg.name, DownloadResult(Status.FAILED, Path(pkg.filename), error="unsafe path"))
                continue

            if pkg.checksum_known and pkg.checksum:
                # Desired state includes packages we have AND ones we still
                # need (failed downloads show up as refs-without-packages).
                pkg_entries.append((pkg.checksum, pkg.repo_path, KIND_PKG))
                if pkg.checksum in pooled_set:
                    summary.pooled += 1
                    if progress:
                        progress(pkg.name, DownloadResult(Status.POOLED, Path(pkg.repo_path)))
                else:
                    to_download.append(pkg)
            elif not pkg.checksum_known and pkg.repo_path in legacy_known:
                pkg_entries.append((legacy_known[pkg.repo_path], pkg.repo_path, KIND_PKG))
                summary.skipped += 1
                if progress:
                    progress(pkg.name, DownloadResult(Status.SKIPPED, Path(pkg.repo_path)))
            else:
                to_download.append(pkg)

        # ── Phase 2: package downloads → pool ─────────────────────────────────
        if phase_callback and not (stop_event and stop_event.is_set()):
            phase_callback(f"Downloading {len(to_download):,} packages...")

        if not dry_run and to_download and not (stop_event and stop_event.is_set()):
            _sem = sem if sem is not None else asyncio.Semaphore(max_connections)

            async def _do_one(pkg: PackageInfo) -> None:
                async with _sem:
                    result = DownloadResult(Status.FAILED, Path(pkg.filename), error="not attempted")
                    for attempt in range(3):
                        if stop_event and stop_event.is_set():
                            summary.stopped = True
                            return  # teardown — not a download failure
                        try:
                            fn = functools.partial(
                                _download_sync, pkg, pool,
                                chunk_callback, start_callback if attempt == 0 else None,
                                stop_event,
                            )
                            result = await loop.run_in_executor(executor, fn)
                        except InterruptedError:
                            summary.stopped = True
                            return  # stopped mid-stream — not a download failure
                        except Exception as exc:
                            result = DownloadResult(
                                Status.FAILED, Path(pkg.filename), error=str(exc) or type(exc).__name__
                            )
                        if result.status != Status.FAILED:
                            break

                match result.status:
                    case Status.DOWNLOADED:
                        summary.downloaded += 1
                        summary.bytes_transferred += result.bytes_transferred
                        if not pkg.checksum_known and result.checksum:
                            # legacy package: identity only known post-download
                            pkg_entries.append((result.checksum, pkg.repo_path, KIND_PKG))
                    case Status.FAILED:
                        summary.failed += 1
                        summary.errors.append(f"{pkg.name}: {result.error}")

                if progress:
                    progress(pkg.name, result)

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
            # The semaphore is deliberately kept: `SyncApp` passes one shared
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
            release_files = [mf for mf in metadata if mf.content is not None and mf.expected_sha256 is None]
            verified_files = [mf for mf in metadata if mf.expected_sha256 is not None]
            other_files    = [mf for mf in metadata if mf.content is None and mf.expected_sha256 is None]

            # Fetch all verified files — pool-membership check first, network
            # only if absent. One batched lookup, off the event loop, instead of
            # a locked point lookup per file on every worker thread.
            wanted = [mf.expected_sha256 for mf in verified_files if mf.expected_sha256]
            already_pooled = await loop.run_in_executor(
                executor, pool.batch_contains, wanted
            ) if wanted else set()
            fetch_results: list[FetchResult | BaseException] = list(await asyncio.gather(
                *[loop.run_in_executor(executor, _fetch_or_skip, mf, already_pooled)
                  for mf in verified_files],
                return_exceptions=True,
            ))

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
                # Pool freshly fetched verified blobs; ALREADY_DONE blobs are
                # referenced as-is. NOT_FOUND files get no entry.
                for mf, r in zip(verified_files, fetch_results):
                    outcome, content, _reason = r  # type: ignore[misc]
                    if outcome is Fetch.OK and content is not None:
                        await loop.run_in_executor(
                            executor,
                            functools.partial(
                                pool.add, content, mf.expected_sha256, "sha256",
                                _meta_filename(mf.repo_path), len(content),
                            ),
                        )
                    if outcome in (Fetch.OK, Fetch.ALREADY_DONE):
                        meta_entries.append((mf.expected_sha256, mf.repo_path, KIND_META))

                # Other files (Release.gpg, .asc, dist-upgrader, …) in parallel.
                # A failure here is reported but doesn't abort the index — same
                # availability the upstream offers without that file.
                other_results = await asyncio.gather(
                    *[loop.run_in_executor(executor, _fetch_and_pool, mf, pool) for mf in other_files],
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
                    err, checksum = await loop.run_in_executor(executor, _fetch_and_pool, mf, pool)
                    if err:
                        summary.metadata_ok = False
                        summary.metadata_errors.append(err)
                    elif checksum:
                        meta_entries.append((checksum, mf.repo_path, KIND_RELEASE))

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

        # ── Phase 4: reconcile — the atomic commit ────────────────────────────
        if not dry_run and summary.metadata_ok and not summary.stopped:
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

    finally:
        if _owns_executor:
            executor.shutdown(wait=False)

    return summary
