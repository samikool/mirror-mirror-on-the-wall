from pathlib import Path
from types import SimpleNamespace
import asyncio

import pytest

from mmw.core.downloader import DownloadResult, Status
from mmw.core.item_progress import ItemInventory, ItemProgressSession, ItemResult, ItemTracker
from mmw.providers.base import PackageInfo, ProgressUnit


def package(path, ident=""):
    return PackageInfo("same display name", "url", "", "sha256", 0, path, path, item_id=ident)


def result(status, size=0):
    return DownloadResult(status, Path("asset"), size)


def mirror(name):
    return SimpleNamespace(name=name, provider="example")


@pytest.mark.parametrize('statuses, outcome', [
    ([Status.DOWNLOADED, Status.POOLED, Status.SKIPPED], Status.DOWNLOADED),
    ([Status.POOLED, Status.SKIPPED, Status.POOLED], Status.POOLED),
    ([Status.DOWNLOADED, Status.FAILED, Status.DOWNLOADED], Status.FAILED),
])
def test_an_item_finishes_only_after_every_asset_and_preserves_bytes(statuses, outcome):
    files = [package(str(i), 'publisher.extension') for i in range(3)]
    events = []
    tracker = ItemTracker(mirror('one'), files, ProgressUnit('widgets'), events.append)
    assert tracker.counts.total == 1
    for file, status in zip(files[:2], statuses[:2]):
        tracker.complete(file, result(status, 7 if status == Status.DOWNLOADED else 0))
    assert not events
    assert tracker.counts.completed == 0
    tracker.complete(files[2], result(statuses[2], 11 if statuses[2] == Status.DOWNLOADED else 0))
    assert len(events) == 1
    assert events[0].id == 'publisher.extension'
    assert events[0].result.status == outcome
    assert events[0].result.bytes_transferred == (7 if statuses[0] == Status.DOWNLOADED else 0) + (
        11 if statuses[2] == Status.DOWNLOADED else 0)
    assert tracker.counts.completed == 1


def test_ungrouped_packages_with_identical_names_remain_separate():
    files = [package('a.deb'), package('b.deb')]
    tracker = ItemTracker(mirror('one'), files, ProgressUnit())
    tracker.complete(files[0], result(Status.DOWNLOADED))
    assert tracker.counts.total == 2
    assert tracker.counts.downloaded == 1


def test_stopping_after_one_asset_does_not_complete_the_item():
    files = [package('vsix', 'a.b'), package('signature', 'a.b')]
    events = []
    tracker = ItemTracker(mirror('one'), files, ProgressUnit('widgets'), events.append)
    tracker.complete(files[0], result(Status.DOWNLOADED, 100))
    assert not events
    assert tracker.counts.completed == 0


def session(monkeypatch):
    from mmw import providers
    provider = SimpleNamespace(progress_unit=lambda unit: ProgressUnit('widgets', 'shared'))
    monkeypatch.setattr(providers, 'get_provider', lambda _: provider)
    return ItemProgressSession([mirror('one'), mirror('two')])


@pytest.mark.parametrize('last_status', [Status.POOLED, Status.FAILED])
def test_session_deduplicates_shared_items_and_waits_for_later_unit(monkeypatch, last_status):
    progress = session(monkeypatch)
    first = ItemInventory('widgets', ('example', 'shared'), frozenset({'a', 'b'}))
    second = ItemInventory('widgets', ('example', 'shared'), frozenset({'a'}))
    progress.register('one', first)
    progress.complete('one', ItemResult('a', 'a', result(Status.DOWNLOADED)))
    progress.complete('one', ItemResult('b', 'b', result(Status.POOLED)))
    assert progress.totals.completed == 0  # the other unit has not supplied its inventory
    progress.register('two', second)
    assert progress.totals.total == 2
    assert progress.totals.pooled == 1
    assert progress.totals.completed == 1
    progress.complete('two', ItemResult('a', 'a', result(last_status)))
    assert progress.totals.completed == 2
    assert progress.totals.downloaded == (last_status != Status.FAILED)
    assert progress.totals.failed == (last_status == Status.FAILED)


def test_dry_run_shared_item_needs_fetch_if_any_units_asset_is_missing(monkeypatch):
    progress = session(monkeypatch)
    first = ItemInventory('widgets', ('example', 'shared'), frozenset({'a'}), frozenset({'a'}))
    second = ItemInventory('widgets', ('example', 'shared'), frozenset({'a'}))
    progress.classify('one', first)
    progress.classify('two', second)
    assert progress.totals.total == 1
    assert progress.totals.downloaded == 1
    assert progress.totals.pooled == 0


def test_counts_snapshot_is_independent_of_later_events(monkeypatch):
    progress = session(monkeypatch)
    inventory = ItemInventory('widgets', ('example', 'shared'), frozenset({'a'}))
    progress.register('one', inventory)
    progress.register('two', inventory)
    snapshot = progress.snapshot()
    progress.complete('one', ItemResult('a', 'a', result(Status.DOWNLOADED)))
    progress.complete('two', ItemResult('a', 'a', result(Status.DOWNLOADED)))
    assert snapshot[0].completed == 0
    assert progress.snapshot()[0].completed == 1


def test_large_inventory_registration_keeps_event_loop_responsive(monkeypatch):
    progress = session(monkeypatch)
    inventory = ItemInventory('widgets', ('example', 'shared'), frozenset(str(i) for i in range(100_000)))
    async def scenario():
        registration = asyncio.create_task(progress.register_async('one', inventory))
        ticks = 0
        while not registration.done():
            await asyncio.sleep(0)
            ticks += 1
        await registration
        assert ticks > 2
        assert progress.totals.total == 100_000
    asyncio.run(scenario())
