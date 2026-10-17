from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
    item_id: str = ""       # related assets share an ID; empty counts this file as one item


@dataclass(frozen=True)
class ProgressUnit:
    """Provider-owned vocabulary and scope for logical progress.

    A shared scope deduplicates items across sync units. Empty scope keeps
    each unit independent (the existing package-counting behavior).
    """
    label: str = "packages"
    scope: str = ""


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
class SourceField:
    """Pure metadata for a source form; widget factories live in tui/."""
    path: str
    label: str
    kind: str
    choices: tuple[str, ...] = ()
    hint: str = ""


@dataclass(frozen=True)
class Dimension:
    """One named axis a provider's sources are narrowed along. Empty means all.

    `name` is the config / network-select key (plural, `suites`); `unit_key`
    is the coordinate's key in `MirrorConfig.dims` (`suite`). A unit with no
    coordinate on this axis fails a filter on it, unless `absent_matches` —
    a flat apt repository has no arch coordinate and must never be dropped
    from an arch-filtered network. `short` is a compact display name where
    space is tight (network detail); `display` falls back to `name`.
    """
    name: str
    unit_key: str
    label: str
    absent_matches: bool = False
    short: str = ""

    @property
    def display(self) -> str:
        return self.short or self.name


@dataclass
class SourceDescription:
    """What a detail pane shows for one source — plain values, no markup.

    `fields` render in order; a list value is a dimension (empty means *all*).
    Then the TUI adds the unit count, then every on-flag in `flags`, then
    `location_label` above `location`.
    """
    fields: list
    flags: list
    location_label: str
    location: str


@dataclass(frozen=True)
class Site:
    """One nginx server block a provider needs beyond a static block per
    `mirror_base`. `server_name` is exact or `~regex`; `root` a directory under
    the publish root (`$host` allowed) served statically; `proxy` locations go
    to the network's gallery (`/` = everything); `exact` = `location = …`."""
    server_name: str
    root: str | None = None
    proxy: tuple = ()
    exact: bool = False


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
    prune: bool = False
    progress: Any = None
    stop_event: Any = None
    keep_extra: frozenset = frozenset()
    mirror_start_callback: Any = None


class BaseProvider(ABC):
    """One ecosystem's plugin. Core never branches on a provider's name; it
    asks the provider. A provider that can recognise a pasted URL also
    defines `probe(url) -> DetectResult` — the only optional member."""

    name: str = ""
    label: str = ""
    description: str = ""
    dimensions: tuple[Dimension, ...] = ()
    by_hash: bool = False   # publish index files under by-hash/SHA256/ too
    publishes: bool = True  # publish/bundle support
    carries_history: bool = False  # fetch_all reads `history`; refetches must pass it too
    # Sync saves this provider's failed downloads, and `repair_packages`
    # retries them against the unit's saved `meta` refs (`mmw retry-failed`).
    retries_failures: bool = False
    fields: tuple[SourceField, ...] = ()

    def progress_unit(self, mirror) -> ProgressUnit:
        return ProgressUnit()

    def repair_packages(self, unit, pool, *, status_callback=None, stop_event=None):
        """`(packages, witness)` to retry without asking upstream: the saved
        failures and their retry token, or — for a unit synced before failures
        were saved — every missing file and the `pool.meta_refs` it came from.
        Only for providers with `retries_failures`."""
        raise NotImplementedError(f"{self.name} cannot retry failed downloads")

    def dimension(self, name: str) -> Dimension | None:
        return next((d for d in self.dimensions if d.name == name), None)

    # ── presentation (pure, Textual-free; the TUI renders these) ─────────────

    def group(self, source) -> str:
        """The heading a source is listed under (apt: its host)."""
        return (urlparse(source.identity).hostname or "?").lower()

    def search_text(self, source) -> str:
        return " ".join((source.name, source.label, source.identity, self.label))

    def source_summary(self, source) -> str:
        arches = getattr(source, "arches", [])
        return ", ".join(arches) if arches else "all arches"

    @abstractmethod
    def describe(self, source) -> SourceDescription:
        """A source's detail pane."""

    def unit_label(self, unit) -> tuple[str, str] | None:
        """(main, dimmed) text for a unit's row, or None for its name."""
        return None

    def unit_describe(self, unit) -> list[tuple[str, str]]:
        """A unit's detail-pane coordinates, in display order."""
        return []

    def expansion_known(self, source) -> bool:
        """Can `expand(source)` list this source's units offline? False when an
        open dimension waits for a sync to resolve it."""
        return True

    # ── config ──────────────────────────────────────────────────────────────

    def merge(self, existing, new):
        """Fold `new` into an already-configured source with the same identity.
        Default: `new` replaces it (a provider whose sources are a selection
        policy has nothing to union)."""
        return new

    @abstractmethod
    def to_toml(self, source) -> dict:
        """The source's minimal TOML form (what `mmw.toml` stores)."""

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
        history=None,
    ) -> tuple[list[PackageInfo], list[MetadataFile]]:
        """Fetch packages and metadata from one consistent upstream snapshot.

        `history` is the unit's previous refs (`mmw.core.history.UnitHistory`);
        providers that carry state forward read it, others ignore it.

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

    def publish(self, ctx: PublishContext, units: list) -> tuple[list, set[str]]:
        """Lay this provider's `units` into `ctx.sink`; return (per-group
        results, the bases published). Default: one group per `mirror_base`,
        packages → indexes → release files, then a prune of that base.

        Nested-base protection (a base inside another must not be pruned by
        it) spans this provider's units only: providers publish under
        disjoint hosts — apt repositories vs Microsoft's — in practice."""
        from mmw.core.publisher.links import publish_group
        groups: dict[str, list] = {}
        for unit in units:
            groups.setdefault(unit.mirror_base, []).append(unit)
        all_bases = set(groups)
        results = []
        for idx, (base, members) in enumerate(groups.items()):
            if ctx.stop_event and ctx.stop_event.is_set():
                break
            if ctx.mirror_start_callback:
                ctx.mirror_start_callback(members[0].name, idx + 1, len(groups))
            nested = {b for b in all_bases if b != base and b.startswith(base + "/")}
            results.append(publish_group(
                members, ctx.sink, ctx.pool, progress=ctx.progress, stop_event=ctx.stop_event,
                prune=ctx.prune, prune_exclude=nested, keep_extra=set(ctx.keep_extra)))
        return results, all_bases

    def preview(self, network, cfg, pool, mirror_dir, result) -> None:
        """Dry-run additions to a `NetworkPublishResult` (read-only)."""
        return None

    site_per_base: bool = True   # nginx: one static block per unit `mirror_base`

    def nginx_sites(self, units) -> list:
        """Server blocks beyond the per-`mirror_base` ones (`Site`s)."""
        return []

    def cert_names(self, network, units) -> list[str]:
        """Names the network's certificate must cover beyond its units' own
        hosts (wildcards allowed). Default: the extra hosts."""
        return self.extra_hosts(network, units)

    def contribute(self, ctx: PublishContext) -> None:
        """Publish-time additions through `ctx.sink` (called for every
        registered provider on every network walk; decide from `ctx`)."""
        return None
