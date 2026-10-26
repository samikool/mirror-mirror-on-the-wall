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
                await until(pilot, lambda c=card: '0/1 widgets' in c.query_one('#stats').render_line(0).text)
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
            assert len(network_bytes) == (1 if fails else 2)  # each successful asset fetched once across both units
            assert all(card._in_flight == 0 for card in view._cards.values())
            assert 'Widgets' in str(view.query_one(StatsFooter).render())
            assert 'files' in str(view.query_one(StatsFooter).render())
    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_waiting_unit_is_not_marked_stalled_from_its_previous_transfer(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from mmw.tui.sync import MirrorCard
    card = MirrorCard(config.MirrorConfig(name='one', provider='apt', source_url='https://example.test/apt'))
    rendered = []
    monkeypatch.setattr(card, 'query_one', lambda *args, **kwargs: SimpleNamespace(update=rendered.append))
    card._total, card._downloaded = 100, 20
    card._start, card._last_chunk_time = 1, 2
    card._refresh_stats(100)
    assert 'ETA --:--' not in rendered[-1]
    card.inc_in_flight()
    card._last_chunk_time = 2
    card._refresh_stats(100)
    assert 'ETA --:--' in rendered[-1]


def test_a_step_after_the_packages_keeps_its_status_line_alive(monkeypatch):
    """The database save follows the last package: its text must replace the
    last package line and keep spinning, not sit there looking frozen."""
    from types import SimpleNamespace
    from mmw.tui import sync as tsync
    card = tsync.MirrorCard(config.MirrorConfig(name='one', provider='apt',
                                                source_url='https://example.test/apt'))
    lines = []
    pkg_line = SimpleNamespace(update=lines.append)
    other = SimpleNamespace(update=lambda text: None, advance=lambda n: None)
    monkeypatch.setattr(card, 'query_one',
                        lambda selector, *args, **kwargs: pkg_line if selector == "#pkg" else other)
    card._start, card._total = 1.0, 2
    card._pending_downloaded, card._pending_pkg = 2, "last.deb"
    card.flush()
    assert lines[-1] == "  → last.deb"
    clock = [100.0]
    monkeypatch.setattr(tsync.time, "monotonic", lambda: clock[0])
    card.set_status("Saving pool references to the database...")
    card.flush()
    first = lines[-1]
    clock[0] += 5
    card.flush()
    assert "Saving pool references to the database..." in lines[-1] and "· 5s" in lines[-1]
    assert lines[-1] != first                       # the spinner moved
    card._pending_downloaded, card._pending_pkg = 1, "next.deb"
    card.flush()
    card.flush()
    assert lines[-1] == "  → next.deb"              # a package line ends the step's turn


def test_the_selected_card_never_looks_done():
    """Green says a mirror finished syncing; the selected card has its own
    look, so a finished card that is selected is still told apart."""
    import asyncio

    from textual.app import ComposeResult

    from mmw.tui import sync as tsync
    from mmw.tui.ui.base import ThemedApp

    def card(name):
        return tsync.MirrorCard(config.MirrorConfig(name=name, provider='apt',
                                                    source_url=f'https://example.test/{name}'))

    class Host(ThemedApp):
        def compose(self) -> ComposeResult:
            yield card("one")
            yield card("two")

    async def run():
        app = Host()
        async with app.run_test() as pilot:
            one, two = app.query(tsync.MirrorCard)
            one.add_class("done")
            two.add_class("done")
            one.focus()
            await pilot.pause()
            assert one.styles.border_top != two.styles.border_top
            from textual.color import Color
            assert two.styles.border_top[1] == Color.parse(app.get_css_variables()["mmw-ok"])
    asyncio.run(run())
