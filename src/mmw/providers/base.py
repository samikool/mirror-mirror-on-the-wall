from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass

import httpx


@dataclass
class PackageInfo:
    name: str
    url: str
    checksum: str           # empty string if not known until download
    checksum_type: str      # "sha256"
    size: int               # 0 if unknown
    repo_path: str          # relative path within repo
    filename: str
    checksum_known: bool = True


@dataclass
class MetadataFile:
    url: str
    repo_path: str          # relative path within repo
    content: bytes | None = None  # pre-fetched content; skips the GET in _fetch_one_metadata
    expected_sha256: str | None = None  # if set, content is verified before writing
    fallback_url: str | None = None     # canonical URL to try if url (by-hash) is exhausted


class BaseProvider(ABC):

    @abstractmethod
    def fetch_packages(
        self, mirror_config, client: httpx.Client,
        status_callback: Callable[[str], None] | None = None,
    ) -> list[PackageInfo]:
        """Return all packages in this mirror."""
        ...

    @abstractmethod
    def fetch_metadata(
        self, mirror_config, client: httpx.Client
    ) -> list[MetadataFile]:
        """Return metadata files to sync exactly as-is (signatures preserved)."""
        ...

    def fetch_all(
        self, mirror_config, client: httpx.Client,
        status_callback: Callable[[str], None] | None = None,
    ) -> tuple[list[PackageInfo], list[MetadataFile]]:
        """Fetch packages and metadata from one consistent upstream snapshot.

        Default: sequential calls.  Providers whose package list and metadata
        derive from a single index override this so both
        views come from the same fetch — two separate GETs can straddle a CDN
        update and describe different snapshots.
        """
        packages = self.fetch_packages(mirror_config, client, status_callback=status_callback)
        metadata = self.fetch_metadata(mirror_config, client)
        return packages, metadata
