"""Logical item progress, independent of providers' file layouts and any UI."""
import asyncio
from dataclasses import dataclass, replace
from pathlib import Path

from mmw.core.downloader import DownloadResult, Status
from mmw.providers.base import PackageInfo, ProgressUnit


@dataclass(frozen=True)
class ItemInventory:
    label: str
    scope: tuple[str, str]
    ids: frozenset[str]
    pooled: frozenset[str] = frozenset()  # dry-run: every asset already available


@dataclass
class ItemCounts:
    label: str
    total: int = 0
    downloaded: int = 0
    pooled: int = 0
    failed: int = 0

    @property
    def completed(self) -> int:
        return self.downloaded + self.pooled + self.failed


@dataclass(frozen=True)
class ItemResult:
    id: str
    name: str
    result: DownloadResult


@dataclass
class _Group:
    required: int = 0
    finished: int = 0
    downloaded: bool = False
    failed: bool = False
    bytes_transferred: int = 0
    error: str = ""
    counted: bool = False

    def add(self, result: DownloadResult) -> None:
        self.finished += 1
        self.downloaded |= result.status == Status.DOWNLOADED
        self.failed |= result.status == Status.FAILED
        self.bytes_transferred += result.bytes_transferred
        self.error = self.error or result.error

    @property
    def status(self) -> Status:
        if self.failed:
            return Status.FAILED
        return Status.DOWNLOADED if self.downloaded else Status.POOLED


def item_id(package: PackageInfo) -> str:
    return package.item_id or package.repo_path


def item_scope(mirror, unit: ProgressUnit) -> tuple[str, str]:
    return mirror.provider, unit.scope or mirror.name


def _count(counts: ItemCounts, group: _Group) -> None:
    if group.failed:
        counts.failed += 1
    elif group.downloaded:
        counts.downloaded += 1
    else:
        counts.pooled += 1


class ItemTracker:
    """Aggregate terminal file results; one item event after all its assets.

    Called by the engine on the event loop, after file workers return. State
    scales with logical items, not with the number of assets.
    """
    def __init__(self, mirror, packages: list[PackageInfo], unit: ProgressUnit, callback=None):
        self.groups: dict[str, _Group] = {}
        self.names: dict[str, str] = {}
        self.callback = callback
        for package in packages:
            ident = item_id(package)
            self.groups.setdefault(ident, _Group()).required += 1
            self.names.setdefault(ident, package.item_id or package.name)
        self.inventory = ItemInventory(unit.label, item_scope(mirror, unit), frozenset(self.groups))
        self.counts = ItemCounts(unit.label, total=len(self.groups))

    def complete(self, package: PackageInfo, result: DownloadResult) -> None:
        ident = item_id(package)
        group = self.groups[ident]
        group.add(result)
        if group.finished == group.required:
            _count(self.counts, group)
            if self.callback:
                self.callback(ItemResult(ident, self.names[ident], DownloadResult(
                    group.status, Path(result.path), group.bytes_transferred, group.error)))


class ItemProgressSession:
    """Deduplicate items across units in a provider-declared scope.

    A shared item's outcome waits for every unit's inventory and all its
    member results. Snapshotting costs only the number of labels, so a TUI
    tick never scans a million files (or even a hundred thousand items).
    """
    def __init__(self, mirrors):
        from mmw.providers import get_provider
        self.expected: dict[tuple[str, str], set[str]] = {}
        self.registered: dict[tuple[str, str], set[str]] = {}
        self.groups: dict[tuple[str, str], dict[str, _Group]] = {}
        self.counts: dict[str, ItemCounts] = {}
        self.labels: dict[tuple[str, str], str] = {}
        self.inventories: dict[str, ItemInventory] = {}
        for mirror in mirrors:
            unit = get_provider(mirror.provider).progress_unit(mirror)
            scope = item_scope(mirror, unit)
            self.expected.setdefault(scope, set()).add(mirror.name)

    def _register(self, mirror_name: str, inventory: ItemInventory):
        if mirror_name in self.inventories:
            raise ValueError(f"inventory already registered: {mirror_name}")
        self.inventories[mirror_name] = inventory
        scope = inventory.scope
        self.labels[scope] = inventory.label
        counts = self.counts.setdefault(inventory.label, ItemCounts(inventory.label))
        groups = self.groups.setdefault(scope, {})
        for index, ident in enumerate(inventory.ids):
            if index and index % 1000 == 0:
                yield
            if ident not in groups:
                groups[ident] = _Group()
                counts.total += 1
            groups[ident].required += 1
        self.registered.setdefault(scope, set()).add(mirror_name)
        if self.registered[scope] == self.expected[scope]:
            for index, group in enumerate(groups.values()):
                if index and index % 1000 == 0:
                    yield
                self._finish(scope, group)

    def register(self, mirror_name: str, inventory: ItemInventory) -> None:
        for _ in self._register(mirror_name, inventory):
            pass

    async def register_async(self, mirror_name: str, inventory: ItemInventory) -> None:
        for _ in self._register(mirror_name, inventory):
            await asyncio.sleep(0)

    def complete(self, mirror_name: str, event: ItemResult) -> None:
        scope = self.inventories[mirror_name].scope
        group = self.groups[scope][event.id]
        group.add(event.result)
        self._finish(scope, group)

    def _finish(self, scope, group):
        if (not group.counted and group.finished == group.required
                and self.registered[scope] == self.expected[scope]):
            _count(self.counts[self.labels[scope]], group)
            group.counted = True

    def classify(self, mirror_name: str, inventory: ItemInventory) -> None:
        self.register(mirror_name, inventory)
        for ident in inventory.ids:
            status = Status.POOLED if ident in inventory.pooled else Status.DOWNLOADED
            self.complete(mirror_name, ItemResult(ident, ident, DownloadResult(status, Path())))

    async def classify_async(self, mirror_name: str, inventory: ItemInventory) -> None:
        await self.register_async(mirror_name, inventory)
        for index, ident in enumerate(inventory.ids):
            if index and index % 1000 == 0:
                await asyncio.sleep(0)
            status = Status.POOLED if ident in inventory.pooled else Status.DOWNLOADED
            self.complete(mirror_name, ItemResult(ident, ident, DownloadResult(status, Path())))

    def snapshot(self) -> list[ItemCounts]:
        return [replace(counts) for counts in self.counts.values()]

    @property
    def totals(self) -> ItemCounts:
        labels = set(self.counts)
        label = next(iter(labels)) if len(labels) == 1 else "items" if labels else "packages"
        return ItemCounts(label, sum(c.total for c in self.counts.values()),
                          sum(c.downloaded for c in self.counts.values()),
                          sum(c.pooled for c in self.counts.values()),
                          sum(c.failed for c in self.counts.values()))
