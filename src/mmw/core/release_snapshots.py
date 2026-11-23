"""Run-scoped APT trust roots, shared across component/architecture workers."""
import threading
from dataclasses import replace
from urllib.parse import urlsplit

from mmw.providers.base import MetadataFile


def fetch_release_files(client, dist_base: str) -> list[MetadataFile]:
    """Read the trust roots together; never cache an unusable Release."""
    files, failures = [], []
    suite_path = urlsplit(dist_base).path.rsplit('/dists/', 1)[-1]
    for name in ('InRelease', 'Release', 'Release.gpg'):
        url = f'{dist_base}/{name}'
        try:
            response = client.get(url)
        except Exception as e:
            failures.append(f'{name}: {type(e).__name__}')
            continue
        if response.status_code != 200:
            failures.append(f'{name}: http {response.status_code}')
            continue
        files.append(MetadataFile(url=url, repo_path=f'dists/{suite_path}/{name}',
                                  content=response.content, is_release=True))
    if not release_content(files):
        raise RuntimeError(
            f'no usable Release file at {dist_base} — '
            + ('; '.join(failures) if failures else 'nothing served'))
    return files


def release_content(files: list[MetadataFile]) -> bytes:
    return next((f.content for f in files
                 if f.is_release and not f.repo_path.endswith('.gpg') and f.content), b'')


class ReleaseSnapshots:
    """Fetch once per suite, with at most one stale-index refresh per run.

    Returned records are private copies: providers attach fetched index bytes
    to metadata, so mutable records must never be shared between workers.
    """
    def __init__(self):
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._files: dict[str, list[MetadataFile]] = {}
        self._refreshed: set[str] = set()

    def _lock(self, key):
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def get(self, client, dist_base: str) -> list[MetadataFile]:
        key = dist_base.rstrip('/')
        with self._lock(key):
            if key not in self._files:
                self._files[key] = fetch_release_files(client, key)
            return [replace(f) for f in self._files[key]]

    def refresh(self, client, dist_base: str, seen: bytes) -> list[MetadataFile]:
        key = dist_base.rstrip('/')
        with self._lock(key):
            cached = self._files.get(key)
            if cached is None or (release_content(cached) == seen and key not in self._refreshed):
                # Mark the attempt before I/O: a failing upstream must not send
                # each waiting unit through another round of GETs.
                self._refreshed.add(key)
                self._files[key] = fetch_release_files(client, key)
            return [replace(f) for f in self._files[key]]
