"""The sync UI consumes a generic provider seam, including grouped assets."""
import asyncio
import hashlib
import threading

import httpx
import pytest

from mmw.core import config, downloader, sync_engine
from mmw.providers.base import PackageInfo, ProgressUnit
from mmw.tui.app import MmwApp
from mmw.tui.sync import StatsFooter
from waiting import until


@pytest.mark.parametrize('fails', [False, True])
@pytest.mark.parametrize('size', [(80, 24), (120, 40)])
def test_grouped_progress_keeps_file_transfers_and_deduplicates_units(tmp_path, monkeypatch, fails, size):
    from mmw.providers import get_provider
    provider = get_provider('apt')
    monkeypatch.setattr(provider, 'progress_unit', lambda mirror: ProgressUnit('widgets', mirror.source))
    packages = [PackageInfo(name=path, url='https://example.test/' + path,
                            checksum=hashlib.sha256(path.encode()).hexdigest(), checksum_type='sha256',
                            size=len(path), repo_path=path, filename=path, item_id='one.widget')
                for path in ['payload', 'signature']]
    monkeypatch.setattr(sync_engine, 'fetch_all_threaded', lambda *a, **kw: (packages, []))
    blocked, release = threading.Event(), threading.Event()
    network_bytes = []
    def respond(request):
        path = request.url.path.lstrip('/')
        if path == 'signature':
            blocked.set()
            assert release.wait(15)
            if fails:
                return httpx.Response(404)
        body = path.encode()
        network_bytes.append(len(body))
        return httpx.Response(200, content=body)
    monkeypatch.setattr(downloader, '_thread_client',
                        lambda: httpx.Client(transport=httpx.MockTransport(respond)))
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path/'mirror', max_connections=4),
                        sources=[config.SourceConfig(url='https://example.test/apt', suites=['stable'],
                                                     components=['main'], arches=['amd64', 'arm64'])])
    async def scenario():
        app = MmwApp(cfg, tmp_path, initial_section='sync')
        async with app.run_test(size=size) as pilot:
            await app.start_sync([])
            view = app.active_sync
            await until(pilot, blocked.is_set)
            await until(pilot, lambda: all(card._start is not None for card in view._cards.values()))
            await until(pilot, lambda: sum(c.bytes_transferred for c in view._cards.values()) > 0)
            assert view._items.totals.total == 1
            assert view._items.totals.completed == 0
            for card in view._cards.values():
                assert card._total == 1
                assert card._item_label == 'widgets'
                assert not card._downloaded + card._pending_downloaded
                assert '0/1 widgets' in card.query_one('#stats').render_line(0).text
            assert sum(c._in_flight for c in view._cards.values()) > 0
            release.set()
            await until(pilot, lambda: not view.running)
            await until(pilot, lambda: '1/1' in str(view.query_one(StatsFooter).render()))
            assert view._items.totals.completed == view._items.totals.total == 1
            assert view._items.totals.failed == int(fails)
            if fails:
                assert view.item_failures == 1  # one widget, despite two unit failures
                assert all(card.has_class('error') for card in view._cards.values())
                await until(pilot, lambda: 'failed widgets: 1' in str(app.screen.query_one('#page-status').render()))
            assert sum(c.bytes_transferred for c in view._cards.values()) == sum(network_bytes)
            assert 'Widgets' in str(view.query_one(StatsFooter).render())
            assert 'files' in str(view.query_one(StatsFooter).render())
    try:
        asyncio.run(scenario())
    finally:
        release.set()
