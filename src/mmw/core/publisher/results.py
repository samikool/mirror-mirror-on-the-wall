from dataclasses import dataclass, field
from pathlib import Path

from mmw.core.format import fmt_timings


class _FoldedErrors:
    """`errors` as a field you can still pass to `__init__`, but which reads
    back as own-errors + every mirror's errors.

    `NetworkPublishResult.errors` is a plain read-only property folding
    `self.mirrors`; `BundleResult.errors` was a plain list that did not, so a
    network selecting a never-synced repository produced
    `"not synced yet — no pool refs for this mirror"` inside
    `result.mirrors[i].errors`, bundled without it, printed `sent=…` and
    exited 0 — the silent-failure shape every other headless verb here is
    written against.

    A bare property can't be used: `bundle_network` (and two TUI tests
    outside this package) construct `BundleResult(..., errors=[...])`, and a
    dataclass can't have both a field and a same-named property. A
    descriptor default is the documented dataclass escape hatch for exactly
    this — `__init__` assigns through `__set__` (storing only this result's
    OWN errors), reads go through `__get__` (folding the mirrors in).
    `own_errors` exposes the underlying mutable list for appenders.
    """

    def __set_name__(self, owner, name: str) -> None:
        self._attr = "_" + name

    def __get__(self, obj, objtype=None):
        if obj is None:
            # Dataclass reads this to discover the field default; returning
            # `self` makes the sentinel `__set__` recognises below.
            return self
        return list(getattr(obj, self._attr)) + [e for r in obj.mirrors for e in r.errors]

    def __set__(self, obj, value) -> None:
        if value is self:      # the "caller passed nothing" sentinel
            value = []
        object.__setattr__(obj, self._attr, list(value))


@dataclass
class MirrorPublishResult:
    mirror_name: str
    linked: int = 0
    linked_bytes: int = 0
    skipped: int = 0
    skipped_bytes: int = 0
    pruned: int = 0
    pruned_bytes: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_files(self) -> int:
        return self.linked + self.skipped

    @property
    def total_bytes(self) -> int:
        return self.linked_bytes + self.skipped_bytes


@dataclass(frozen=True)
class NetworkTotals:
    """A dry run's network-wide numbers, each file counted once. Mirrors
    share files, so these are not the sum of the per-mirror rows."""
    files: int
    bytes: int
    published_files: int
    published_bytes: int


@dataclass
class NetworkPublishResult:
    network_name: str
    mirrors: list[MirrorPublishResult] = field(default_factory=list)
    dry_run: bool = False
    dist_upgrade_paths: list[tuple[str, str]] = field(default_factory=list)
    dist_upgrade_warnings: list[str] = field(default_factory=list)
    # Anything else worth telling that is not a failure (a missing gallery
    # image): printed and notified like dist-upgrade warnings, never sets `ok`.
    warnings: list[str] = field(default_factory=list)
    pool_pruned: int = 0
    pool_pruned_bytes: int = 0
    # Files removed by the network-wide reconcile steps that belong to no
    # single mirror group: a whole `mirror_base` subtree dropped from
    # `[[networks.select]]`, and stale `nginx/sites/*.conf` / `keys/` files.
    # Kept off `mirrors` so they never appear as a phantom per-mirror row,
    # but folded into the `pruned` totals below so the reported count is the
    # truth about what publish deleted.
    extra_pruned: int = 0
    extra_pruned_bytes: int = 0
    # Every base the walk published (`BaseProvider.publish` decides them);
    # recorded in snapshots so replay and bundles need not re-derive them.
    bases: list[str] = field(default_factory=list)
    # Set by a dry run: the totals below then count shared files once
    # instead of summing the per-mirror estimates.
    totals: NetworkTotals | None = None
    # Seconds per phase worth reporting (`disk <route>` from the job's lanes).
    timings: dict[str, float] = field(default_factory=dict)

    def timing_summary(self) -> str:
        return fmt_timings(self.timings)

    @property
    def linked(self) -> int:
        if self.totals is not None:
            return max(0, self.totals.files - self.totals.published_files)
        return sum(r.linked for r in self.mirrors)

    @property
    def linked_bytes(self) -> int:
        if self.totals is not None:
            return max(0, self.totals.bytes - self.totals.published_bytes)
        return sum(r.linked_bytes for r in self.mirrors)

    @property
    def skipped(self) -> int:
        if self.totals is not None:
            return min(self.totals.files, self.totals.published_files)
        return sum(r.skipped for r in self.mirrors)

    @property
    def skipped_bytes(self) -> int:
        if self.totals is not None:
            return min(self.totals.bytes, self.totals.published_bytes)
        return sum(r.skipped_bytes for r in self.mirrors)

    @property
    def pruned(self) -> int:
        return sum(r.pruned for r in self.mirrors) + self.pool_pruned + self.extra_pruned

    @property
    def pruned_bytes(self) -> int:
        return (sum(r.pruned_bytes for r in self.mirrors)
                + self.pool_pruned_bytes + self.extra_pruned_bytes)

    @property
    def total_files(self) -> int:
        return self.linked + self.skipped

    @property
    def total_bytes(self) -> int:
        return self.linked_bytes + self.skipped_bytes

    @property
    def errors(self) -> list[str]:
        return [e for r in self.mirrors for e in r.errors]


@dataclass
class BundleResult:
    """Mirrors `NetworkPublishResult`'s shape, but for one bundle `.tar`.

    `errors` folds every group's `MirrorPublishResult.errors` in exactly as
    `NetworkPublishResult.errors` does — see `_FoldedErrors` above for why it
    is a descriptor rather than a plain property, and for the silent-success
    bug that folding fixes. Errors bundling itself raises (a key that would
    not link, a stopped run, a close()-time blob failure) have no per-group
    result to live in, so `bundle_network` appends them to `own_errors`.
    """
    network_name: str
    out_path: Path | None = None
    created: str = ""
    previous: str | None = None
    full: bool = False
    sent: int = 0
    sent_bytes: int = 0
    deleted: int = 0
    manifest_path: Path | None = None
    checksum_path: Path | None = None
    mirrors: list[MirrorPublishResult] = field(default_factory=list)
    errors: list[str] = _FoldedErrors()
    dist_upgrade_warnings: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)

    def timing_summary(self) -> str:
        return fmt_timings(self.timings)

    @property
    def own_errors(self) -> list[str]:
        """The mutable list behind `errors`, holding only this bundle's own.

        `result.errors.append(...)` would silently vanish — `errors` builds a
        fresh folded list on every read — so every appender goes through here.
        """
        return self._errors

    @property
    def total_files(self) -> int:
        return sum(r.total_files for r in self.mirrors)
