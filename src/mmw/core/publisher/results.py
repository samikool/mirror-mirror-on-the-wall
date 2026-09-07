from dataclasses import dataclass, field
from pathlib import Path


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


@dataclass
class NetworkPublishResult:
    network_name: str
    mirrors: list[MirrorPublishResult] = field(default_factory=list)
    dry_run: bool = False
    dist_upgrade_paths: list[tuple[str, str]] = field(default_factory=list)
    dist_upgrade_warnings: list[str] = field(default_factory=list)
    pool_pruned: int = 0
    pool_pruned_bytes: int = 0

    @property
    def linked(self) -> int:
        return sum(r.linked for r in self.mirrors)

    @property
    def linked_bytes(self) -> int:
        return sum(r.linked_bytes for r in self.mirrors)

    @property
    def skipped(self) -> int:
        return sum(r.skipped for r in self.mirrors)

    @property
    def skipped_bytes(self) -> int:
        return sum(r.skipped_bytes for r in self.mirrors)

    @property
    def pruned(self) -> int:
        return sum(r.pruned for r in self.mirrors) + self.pool_pruned

    @property
    def pruned_bytes(self) -> int:
        return sum(r.pruned_bytes for r in self.mirrors) + self.pool_pruned_bytes

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
    """Mirrors `NetworkPublishResult`'s shape, but for one `.tar.zst`.

    Unlike `NetworkPublishResult.errors` (a property folding every group's own
    `MirrorPublishResult.errors`), `errors` here is a plain list: bundling has
    no per-group result to attribute a key or dist-upgrade failure to, so
    `bundle_network` appends directly.
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
    mirrors: list[MirrorPublishResult] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dist_upgrade_warnings: list[str] = field(default_factory=list)

    @property
    def total_files(self) -> int:
        return sum(r.total_files for r in self.mirrors)
