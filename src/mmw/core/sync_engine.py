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
from mmw.core import upstream
from mmw.core.keys import KeySyncResult, sync_custom_key, write_key
from mmw.core.pool import Pool
from mmw.core.item_progress import ItemInventory, ItemProgressSession, ItemResult, ItemTracker, item_id
from mmw.core.download_coordinator import DownloadCoordinator
from mmw.providers import get_provider


@dataclass
class SyncEvents:
    """Callbacks for rendering sync progress.

    All optional. `phase`, `progress`, `chunk`, and `download_started` may be
    invoked from worker threads — implementations must be fast and thread-safe
    (post a message / bump a locked counter; never touch UI state directly).
    """
    phase: Callable[[str], None] | None = None          # human-readable status line
    started: Callable[[int], None] | None = None        # legacy file count, when items_started is absent
    progress: ProgressCallback | None = None            # per-package outcome
    chunk: ChunkCallback | None = None                  # per 64 KB network chunk
    download_started: Callable[[], None] | None = None  # a thread began an actual download
    items_started: Callable[[ItemInventory], None] | None = None
    item_progress: Callable[[ItemResult], None] | None = None  # every asset of an item has finished
    download_finished: Callable[[], None] | None = None  # matches download_started, independent of pooled outcomes


@dataclass
class DryRunStats:
    total: int
    in_pool: int
    to_fetch: int
    fetch_size: int
    has_unknown: bool            # some package to fetch has no stated size
    total_size: int = 0
    pool_size: int = 0
    no_checksum: int = 0         # to fetch, with no checksum to look up
    sizes_known: bool = True     # every package states its size
    item_label: str = "packages"
    inventory: ItemInventory | None = None


class KeySyncCoordinator:
    """Fetches each repository's GPG key and upstream files once.

    There is no distro catalog to scrape well-known key locations from any
    more: a key is mirrored when the source declares one (`gpg_key`, or a
    `gpg_key_url` to fetch). The key URL and every `extra_files` URL are also
    kept byte-for-byte (`core/upstream.py`) for publish to serve at their own
    path. Work is claimed once across a repository's concurrently syncing
    mirrors — ubuntu-ports alone expands to 16.
    """

    def __init__(self, mirror_dir: Path):
        self._mirror_dir = mirror_dir
        self._lock = threading.Lock()
        self._claimed: set[str] = set()
        self.results: dict[str, KeySyncResult] = {}

    def _claim(self, claim: str) -> bool:
        with self._lock:
            if claim in self._claimed:
                return False
            self._claimed.add(claim)
            return True

    def sync_for(self, mirror: MirrorConfig) -> list[str]:
        """Blocking — run in an executor thread. Returns failure descriptions."""
        failures, raw = self._sync_key(mirror)
        failures += self._sync_upstream(mirror, raw)
        return failures

    def _sync_key(self, mirror: MirrorConfig) -> tuple[list[str], bytes | None]:
        """keys/{label}/: an inline key (deb822 `Signed-By:`) needs no fetch; a
        `gpg_key_url` does. Returns failures and the URL's bytes, if fetched."""
        inline = getattr(mirror, "gpg_key", None)
        url = mirror.gpg_key_url
        if not (inline or url):
            return [], None

        # Keys are grouped by **label** (the host's domain), not by expanded
        # mirror name. ubuntu-ports alone expands to 16 mirrors, so the
        # name-keyed layout wrote 16 byte-identical copies of
        # `ubuntu-archive-keyring.asc` into 16 directories — and published none
        # of them, because publish reads the pool and the pool has no key
        # column. A label directory is one place, and `label` *is* in the pool.
        group = mirror.label or upstream.repository_name(mirror)
        stem = getattr(mirror, "gpg_key_name", None) or upstream.repository_name(mirror)
        claim = f"{group}/{stem}" if inline else f"{group}/url:{url}"
        if not self._claim(claim):
            return [], None
        try:
            if inline:
                result = write_key(group, inline, self._mirror_dir, stem)
            else:
                result = sync_custom_key(group, url, self._mirror_dir, _thread_client())
            with self._lock:
                self.results[claim] = result
            return list(result.failed), result.raw
        except Exception as e:
            return [f"{mirror.name} key: {e}"], None

    def _sync_upstream(self, mirror: MirrorConfig, key_raw: bytes | None) -> list[str]:
        """upstream/{repository}/: the key URL and every extra file, as sent.
        Per repository (the derived source name), so a network gets exactly
        the files of the repositories it publishes. A failed fetch keeps the
        last good copy; a URL no longer named is removed."""
        url = mirror.gpg_key_url
        urls = ([url] if url else []) + list(getattr(mirror, "extra_files", []) or [])
        group = upstream.repository_group(mirror)
        if not self._claim(f"upstream:{group}"):
            return []
        failures: list[str] = []
        for u in dict.fromkeys(urls):
            try:
                raw = key_raw if (u == url and key_raw is not None) else \
                    upstream.fetch_bytes(u, _thread_client())
                upstream.store(self._mirror_dir, group, u, raw)
            except Exception as e:
                failures.append(f"{u}: {e}")
        try:
            upstream.prune(self._mirror_dir, group, urls)
        except OSError as e:
            failures.append(f"upstream/{group}: {e}")
        return failures


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
        # The one classification, shared by the sync TUI's dry run and
        # `mmw dry-run` (which used to carry its own copy).
        known = [p.checksum for p in packages if p.checksum]
        pooled = pool.batch_contains(known)
        # A package with no upstream checksum is "had" when the unit's last
        # sync recorded its path — the same rule `download_mirror` applies.
        refmap = pool.ref_map(mirror.name) if any(not p.checksum for p in packages) else {}
        if refmap:
            available = pool.batch_contains(list(refmap.values()))
            refmap = {path: checksum for path, checksum in refmap.items() if checksum in available}
        unknown_sources = [(p.url, p.checksum_type) for p in packages if not p.checksum_known]
        sources = pool.download_checksums(unknown_sources) if unknown_sources else {}
        missing = [p for p in packages
                   if not (p.checksum and p.checksum in pooled)
                   and not (not p.checksum and (p.repo_path in refmap or
                                                (p.url, p.checksum_type.lower()) in sources))]
        total_size = sum(p.size for p in packages)
        fetch_size = sum(p.size for p in missing)
        tracker = ItemTracker(mirror, packages, provider.progress_unit(mirror))
        missing_items = {item_id(p) for p in missing}
        inventory = ItemInventory(tracker.inventory.label, tracker.inventory.scope,
                                  tracker.inventory.ids, tracker.inventory.ids - missing_items)
        return DryRunStats(
            total=len(inventory.ids),
            in_pool=len(inventory.pooled),
            to_fetch=len(missing_items),
            fetch_size=fetch_size,
            has_unknown=any(p.size == 0 for p in missing),
            total_size=total_size,
            pool_size=total_size - fetch_size,
            no_checksum=sum(1 for p in missing if not p.checksum),
            sizes_known=all(p.size > 0 for p in packages),
            item_label=inventory.label,
            inventory=inventory,
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
    items_session: ItemProgressSession | None = None,
    downloads: DownloadCoordinator | None = None,
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
    from mmw.core.history import UnitHistory
    history = await loop.run_in_executor(executor, UnitHistory, pool, mirror.name)
    packages, metadata = await loop.run_in_executor(
        executor,
        lambda: fetch_all_threaded(provider, mirror, status_callback=events.phase,
                                   history=history, stop_event=stop_event),
    )

    def item_finished(event: ItemResult) -> None:
        if items_session is not None:
            items_session.complete(mirror.name, event)
        if events.item_progress:
            events.item_progress(event)

    tracker = await loop.run_in_executor(
        executor, lambda: ItemTracker(mirror, packages, provider.progress_unit(mirror), item_finished))
    if items_session is not None:
        await items_session.register_async(mirror.name, tracker.inventory)
    if events.items_started:
        events.items_started(tracker.inventory)
    elif events.started:
        events.started(len(packages))

    def _refetch_metadata() -> list:
        """Re-read the upstream index. Called only when the snapshot we started
        from went stale during a long package phase. A provider that carries
        state forward needs its history here too, or the refetch drops it."""
        if provider.carries_history:
            return fetch_all_threaded(provider, mirror, history=history, stop_event=stop_event)[1]
        return fetch_metadata_threaded(provider, mirror, stop_event=stop_event)

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
        file_progress=tracker.complete,
        downloads=downloads,
        finish_callback=events.download_finished,
    )
    summary.items = tracker.counts

    if keys is not None and not (stop_event and stop_event.is_set()):
        if events.phase:
            events.phase("Syncing GPG keys...")
        try:
            summary.key_errors = await loop.run_in_executor(executor, keys.sync_for, mirror)
        except Exception as e:
            summary.key_errors = [f"key sync: {e}"]

    return summary
