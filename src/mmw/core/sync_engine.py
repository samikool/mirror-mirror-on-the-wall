"""Shared sync pipeline used by both the headless command and the TUI.

Frontends are presentation-only: they construct a SyncEvents bundle and render
what the engine reports. All fetch/classify/download logic lives here or below
(downloader.py), so behavior can never drift between `mmw sync --quiet` and the
sync TUI again.
"""
import asyncio
import concurrent.futures
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mmw.core.config import MirrorConfig
from mmw.core.downloader import (
    ChunkCallback,
    MirrorSummary,
    ProgressCallback,
    _thread_client,
    download_mirror,
    fetch_all_threaded,
    fetch_metadata_threaded,
    fetch_packages_threaded,
)
from mmw.core.keys import (
    KeySyncResult,
    fetch_key_bytes,
    store_upstream_key,
    sync_custom_key,
    write_key,
)
from mmw.core.pool import Pool
from mmw.providers import get_provider


@dataclass
class SyncEvents:
    """Callbacks for rendering sync progress.

    All optional. `phase`, `progress`, `chunk`, and `download_started` may be
    invoked from worker threads — implementations must be fast and thread-safe
    (post a message / bump a locked counter; never touch UI state directly).
    """
    phase: Callable[[str], None] | None = None          # human-readable status line
    started: Callable[[int], None] | None = None        # package count known; downloads begin
    progress: ProgressCallback | None = None            # per-package outcome
    chunk: ChunkCallback | None = None                  # per 64 KB network chunk
    download_started: Callable[[], None] | None = None  # a thread began an actual download


@dataclass
class DryRunStats:
    total: int
    in_pool: int
    to_fetch: int
    fetch_size: int
    has_unknown: bool


def _key_group(mirror: MirrorConfig) -> str:
    """Fallback naming for a mirror with no label / no `Signed-By:` filename.

    The *source* name, never the expanded mirror name: the latter carries suite,
    component and arch, so one repository would scatter a key across every
    combination of them.
    """
    from mmw.core.config import derive_name

    source = getattr(mirror, "source", "") or mirror.source_url
    return derive_name(source) if source else mirror.name


class KeySyncCoordinator:
    """Fetches each repository's declared GPG key once.

    There is no distro catalog to scrape well-known key locations from any more
    (see docs/history/repository-first-redesign.md); a key is mirrored when the source
    declares `gpg_key_url`. Sources sharing a key URL fetch it once.
    """

    def __init__(self, mirror_dir: Path):
        self._mirror_dir = mirror_dir
        self._lock = threading.Lock()
        self._claimed: set[str] = set()
        self.results: dict[str, KeySyncResult] = {}

    def sync_for(self, mirror: MirrorConfig) -> list[str]:
        """Blocking — run in an executor thread. Returns failure descriptions.

        An inline key (deb822 `Signed-By:`) needs no fetch; a `gpg_key_url` does.
        Either way the work is claimed once across concurrently syncing mirrors.
        """
        inline = getattr(mirror, "gpg_key", None)
        if not (inline or mirror.gpg_key_url):
            return []

        # Keys are grouped by **label** (the host's domain), not by expanded
        # mirror name. ubuntu-ports alone expands to 16 mirrors, so the
        # name-keyed layout wrote 16 byte-identical copies of
        # `ubuntu-archive-keyring.asc` into 16 directories — and published none
        # of them, because publish reads the pool and the pool has no key
        # column. A label directory is one place, and `label` *is* in the pool.
        group = mirror.label or _key_group(mirror)
        stem = getattr(mirror, "gpg_key_name", None) or _key_group(mirror)
        url = mirror.gpg_key_url
        claim = f"{group}/{stem}" if inline else f"{group}/url:{url}"
        with self._lock:
            if claim in self._claimed:
                return []
            self._claimed.add(claim)
        try:
            if inline:
                result = write_key(group, inline, self._mirror_dir, stem)
                # Both declared: the inline key fills keys/, and the URL is
                # still fetched — only for its byte-for-byte upstream copy.
                if url and result.ok:
                    try:
                        raw = fetch_key_bytes(url, _thread_client())
                        store_upstream_key(group, url, raw, self._mirror_dir)
                    except Exception as e:
                        result.failed.append(f"{stem} (upstream copy from {url}): {e}")
            else:
                result = sync_custom_key(group, url, self._mirror_dir, _thread_client())
            with self._lock:
                self.results[claim] = result
            return list(result.failed)
        except Exception as e:
            return [f"{mirror.name} key: {e}"]


async def classify_mirror(
    mirror: MirrorConfig,
    pool: Pool,
    executor: concurrent.futures.Executor,
) -> DryRunStats:
    """Dry-run: fetch the package index and diff it against the pool. No downloads."""
    provider = get_provider(mirror.provider)
    loop = asyncio.get_running_loop()
    packages = await loop.run_in_executor(executor, fetch_packages_threaded, provider, mirror)

    def _classify() -> DryRunStats:
        known = [p.checksum for p in packages if p.checksum]
        pooled = pool.batch_contains(known)
        missing = [p for p in packages if not (p.checksum and p.checksum in pooled)]
        return DryRunStats(
            total=len(packages),
            in_pool=len(packages) - len(missing),
            to_fetch=len(missing),
            fetch_size=sum(p.size for p in missing),
            has_unknown=any(p.size == 0 for p in missing),
        )

    return await loop.run_in_executor(executor, _classify)


async def sync_mirror(
    mirror: MirrorConfig,
    *,
    pool: Pool,
    mirror_dir: Path,
    max_connections: int,
    executor: concurrent.futures.Executor,
    events: SyncEvents | None = None,
    sem: asyncio.Semaphore | None = None,
    stop_event: threading.Event | None = None,
    keys: KeySyncCoordinator | None = None,
) -> MirrorSummary:
    """Full pipeline for one mirror: fetch index → download → metadata → GPG keys.

    Raises on index-fetch failure (the mirror can't be synced at all);
    everything after that is reported through the returned MirrorSummary.
    """
    events = events or SyncEvents()
    provider = get_provider(mirror.provider)
    loop = asyncio.get_running_loop()

    if events.phase:
        events.phase("Fetching metadata...")
    packages, metadata = await loop.run_in_executor(
        executor,
        lambda: fetch_all_threaded(provider, mirror, status_callback=events.phase),
    )

    if events.started:
        events.started(len(packages))

    def _refetch_metadata() -> list:
        """Re-read the upstream index. Called only when the snapshot we started
        from went stale during a long package phase."""
        return fetch_metadata_threaded(provider, mirror)

    # The pool is the authority on what exists; remember what this mirror is so
    # publish can reconstruct it offline without re-resolving architectures.
    await loop.run_in_executor(executor, pool.record_mirror, mirror)

    summary = await download_mirror(
        packages=packages,
        metadata=metadata,
        pool=pool,
        mirror_name=mirror.name,
        source=mirror.source,
        max_connections=max_connections,
        refetch_metadata=_refetch_metadata,
        progress=events.progress,
        phase_callback=events.phase,
        chunk_callback=events.chunk,
        start_callback=events.download_started,
        executor=executor,
        sem=sem,
        stop_event=stop_event,
    )

    if keys is not None and not (stop_event and stop_event.is_set()):
        if events.phase:
            events.phase("Syncing GPG keys...")
        try:
            summary.key_errors = await loop.run_in_executor(executor, keys.sync_for, mirror)
        except Exception as e:
            summary.key_errors = [f"key sync: {e}"]

    return summary
