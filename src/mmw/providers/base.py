from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    # Explicit ref kind. The sync used to infer this from the shape of the
    # other fields — "content is set and expected_sha256 is not" meant a
    # release file — which happened to be true for `AptProvider` (it pre-fetches
    # InRelease/Release/Release.gpg) and false for `AptFlatProvider` (which
    # only HEADs them, so `content` is None). Flat repositories therefore filed
    # their trust roots as KIND_META, and publish orders pkg -> meta -> release
    # specifically so a client mid-`apt update` never sees a release file whose
    # indexes are not there yet. Naming the kind removes the inference.
    is_release: bool = False


@dataclass(frozen=True)
class Dimension:
    """One named axis a provider's sources are narrowed along. Empty means all.

    `name` is the config / network-select key (plural, `suites`); `unit_key`
    is the coordinate's key in `MirrorConfig.dims` (`suite`). A unit with no
    coordinate on this axis fails a filter on it, unless `absent_matches` —
    a flat apt repository has no arch coordinate and must never be dropped
    from an arch-filtered network.
    """
    name: str
    unit_key: str
    label: str
    absent_matches: bool = False


@dataclass
class PublishContext:
    """What a provider's `contribute()` may use while a network is laid out.

    `sink` is the same Sink every group was published through; `result` is
    the network's `NetworkPublishResult`; `units` is every unit the network
    publishes, across all providers.
    """
    network: Any
    cfg: Any
    mirror_dir: Path
    pool: Any
    sink: Any
    result: Any
    units: list


class BaseProvider(ABC):
    """One ecosystem's plugin. Core never branches on a provider's name; it
    asks the provider. A provider that can recognise a pasted URL also
    defines `probe(url) -> DetectResult` — the only optional member."""

    name: str = ""
    label: str = ""
    description: str = ""
    dimensions: tuple[Dimension, ...] = ()
    by_hash: bool = False   # publish index files under by-hash/SHA256/ too

    def dimension(self, name: str) -> Dimension | None:
        return next((d for d in self.dimensions if d.name == name), None)

    # ── config → units ───────────────────────────────────────────────────────

    @abstractmethod
    def expand(self, source) -> list:
        """Offline expansion of a source into units (no network)."""

    @abstractmethod
    def covers(self, source, unit) -> bool:
        """Does `source`, as configured now, still ask for `unit`?"""

    def needs_resolving(self, source) -> bool:
        """True when an open dimension needs the network to expand."""
        return False

    @abstractmethod
    def resolve(self, source, *, progress: Callable[[str], None] | None = None):
        """Fill open dimensions against upstream → `mmw.core.resolve.ResolvedSource`."""

    # ── fetch ────────────────────────────────────────────────────────────────

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

    # ── verify / publish ─────────────────────────────────────────────────────

    def verify_witness(self, unit, pool):
        """An independent second opinion parsed from pooled metadata →
        `mmw.core.verify.VerifyResult`, or None when the provider has none."""
        return None

    def published_index(self, dst_root: Path, unit) -> Path | None:
        """The index file at a publish destination that lists this unit's
        packages (dry-run size accounting reads it), or None."""
        return None

    def published_packages(self, index: Path) -> list[PackageInfo]:
        """The packages a `published_index` file lists."""
        return []

    def extra_hosts(self, network, units) -> list[str]:
        """Hostnames this provider serves beyond its units' own hosts (pure —
        nginx, certs and snapshots all ask)."""
        return []

    def contribute(self, ctx: PublishContext) -> None:
        """Publish-time additions through `ctx.sink` (called for every
        registered provider on every network walk; decide from `ctx`)."""
        return None
