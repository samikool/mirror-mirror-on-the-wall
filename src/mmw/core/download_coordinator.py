"""One download per content identity across a sync's parallel units."""
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
import threading

from mmw.core.downloader import DownloadResult, Status
from mmw.core.pool import Pool
from mmw.providers.base import PackageInfo


DownloadKey = tuple[Pool, str, str, str]
FetchCandidate = Callable[[str, str], Awaitable[DownloadResult | None]]


@dataclass
class _Claim:
    urls: list[str]
    seen: set[str]
    task: asyncio.Task | None = None


class DownloadCoordinator:
    """An event-loop-owned hashmap, shared by all units in one sync.

    Known checksums share a claim regardless of filename or source URL.
    Without a checksum, only exact URLs (and matching hash algorithms) share.
    Completed checksum claims are released: subsequent claims perform an
    indexed pool recheck, keeping memory bounded by active work. Unknown URLs
    retain their computed checksum so later units can perform that same check.
    Neither lookups nor followers scan files or hash their contents.
    """
    def __init__(self):
        self._active: dict[DownloadKey, _Claim] = {}
        self._url_checksums: dict[DownloadKey, str] = {}

    @staticmethod
    def key(pool: Pool, package: PackageInfo) -> DownloadKey:
        known = package.checksum_known and bool(package.checksum)
        return (pool, package.checksum_type.lower(), 'checksum' if known else 'url',
                package.checksum if known else package.url)

    async def run(self, pool: Pool, package: PackageInfo, fetch: FetchCandidate,
                  stop_event: threading.Event | None = None,
                  finished: Callable[[], None] | None = None) -> DownloadResult | None:
        key = self.key(pool, package)
        while not (stop_event and stop_event.is_set()):
            claim = self._active.get(key)
            owner = claim is None
            if owner:
                claim = _Claim([package.url], {package.url})
                self._active[key] = claim
                claim.task = asyncio.create_task(self._fetch(key, claim, package, fetch, finished))
            elif package.url not in claim.seen:
                claim.seen.add(package.url)
                claim.urls.append(package.url)
            task = claim.task
            # A cancelled caller must not cancel the shared download. For a
            # stop event, wait without holding a network permit and check the
            # signal periodically (one task per active consumer, not per file).
            if stop_event is None:
                result = await asyncio.shield(task)
            else:
                while not task.done() and not stop_event.is_set():
                    await asyncio.wait((task,), timeout=0.25)
                if stop_event.is_set():
                    return None
                result = task.result()
            if result is None:
                # The owner stopped. A waiter with a different stop signal can
                # claim the still-needed content with its own fetch context.
                if owner:
                    return None
                continue
            if owner or result.status == Status.FAILED:
                return result
            checksum = result.checksum or package.checksum
            if not checksum:
                return DownloadResult(Status.FAILED, Path(package.filename),
                                      error='shared download did not return a checksum')
            return DownloadResult(Status.POOLED, result.path, checksum=checksum)
        return None

    async def _fetch(self, key, claim, package, fetch, finished):
        result = DownloadResult(Status.FAILED, Path(package.filename), error='no usable source URL')
        try:
            index = 0
            while index < len(claim.urls):
                url = claim.urls[index]
                index += 1
                result = await fetch(url, self._url_checksums.get(key, ''))
                if result is None:
                    return None
                if result.status != Status.FAILED:
                    if key[2] == 'url' and result.checksum:
                        self._url_checksums[key] = result.checksum
                    return result
            return result
        except Exception as exc:
            return DownloadResult(Status.FAILED, Path(package.filename), error=str(exc) or type(exc).__name__)
        finally:
            if self._active.get(key) is claim:
                del self._active[key]
            if finished:
                finished()
