import asyncio
import threading

import pytest

from mmw.core import config
from mmw.tui.app import MmwApp
from waiting import until


def test_resolution_progress_is_visible_second_start_refused_and_cancel_discards_result(tmp_path, monkeypatch):
    from mmw.tui import repositories
    started, release = threading.Event(), threading.Event()
    calls = []
    def resolve(cfg, selection, *, progress=None, stop_event=None, config_dir=None):
        calls.append(selection)
        if progress:
            progress('marketplace: HTTP 429 — retrying in 1s')
        started.set()
        release.wait(10)
        return [], []
    monkeypatch.setattr(repositories, 'resolve_selection', resolve)
    src = config.VscodeSource(provider='vscode', kinds=['server'], platforms=['linux-x64'])
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path/'m'), sources=[src])
    async def scenario():
        app = MmwApp(cfg, tmp_path, initial_section='sync')
        notes = []
        app.notify = lambda text, **kw: notes.append(str(text))
        async with app.run_test() as pilot:
            app.run_worker(app.start_sync([]))
            await until(pilot, started.is_set)
            await until(pilot, lambda: '429' in str(app.screen.query_one('#sync-resolve').render()))
            await app.start_sync([])
            assert len(calls) == 1
            assert any('resolv' in note.lower() for note in notes)
            app.cancel_resolution()
            release.set()
            await until(pilot, lambda: not app._resolving)
            assert app.active_sync is None
            assert 'cancel' in str(app.screen.query_one('#sync-resolve').render()).lower()
    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_gallery_image_reports_cached_phase_and_stop_skips_docker(tmp_path, monkeypatch):
    from mmw.providers.vscode import image
    from test_gallery_image import _fake_docker
    docker, log = _fake_docker(tmp_path)
    monkeypatch.setattr(image, 'DOCKER', docker)
    stop = threading.Event()
    stop.set()
    result = image.gallery_image(tmp_path/'cache', stop_event=stop)
    assert result.path is None
    assert not log.exists()
    image.gallery_image(tmp_path/'cache')
    phases = []
    result = image.gallery_image(tmp_path/'cache', progress=phases.append)
    assert result.path.is_file()
    assert 'cached' in phases
    assert phases[-1] == 'done'


def test_gallery_image_event_does_not_count_as_payload():
    from mmw.tui.transfer import TransferView, PUBLISH
    view = TransferView(PUBLISH, 'test')
    before = view._linked
    view.record('vscode-gallery', 'gallery-image:saving')
    assert view._linked == before
    assert 'Gallery image' in view._phase
    view.record('archive.tar', 'archive-start:1:100')
    view.record('payload', 'linked')
    assert view._linked == before + 1


def test_stop_during_image_save_preserves_previous_cache(tmp_path, monkeypatch):
    from mmw.providers.vscode import image
    from mmw.core import container_image
    from test_gallery_image import _fake_docker
    docker, _ = _fake_docker(tmp_path, 'sha256:old')
    monkeypatch.setattr(image, 'DOCKER', docker)
    cached = image.gallery_image(tmp_path/'cache').path
    docker, _ = _fake_docker(tmp_path, 'sha256:new')
    monkeypatch.setattr(image, 'DOCKER', docker)
    original = container_image._run
    stop = threading.Event()
    def run(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[1] == 'save':
            stop.set()
        return result
    monkeypatch.setattr(container_image, '_run', run)
    result = image.gallery_image(tmp_path/'cache', stop_event=stop)
    assert result.path is None
    assert cached.is_file()
    assert not (tmp_path/'cache'/'sha256-new.tar').exists()
    assert not list((tmp_path/'cache').glob('*.part'))


def test_image_phases_and_stop_between_inspect_and_save(tmp_path, monkeypatch):
    from mmw.providers.vscode import image
    from test_gallery_image import _fake_docker
    docker, log = _fake_docker(tmp_path)
    monkeypatch.setattr(image, 'DOCKER', docker)
    stop = threading.Event()
    phases = []
    def progress(phase):
        phases.append(phase)
        if phase == 'inspecting':
            stop.set()
    result = image.gallery_image(tmp_path/'cache', progress=progress, stop_event=stop)
    assert result.path is None
    assert not any(line.startswith('save ') for line in log.read_text().splitlines())
    stop.clear()
    phases.clear()
    result = image.gallery_image(tmp_path/'cache', progress=phases.append)
    assert result.path.is_file()
    assert phases == ['pulling', 'inspecting', 'saving', 'done']
    monkeypatch.setattr(image, 'DOCKER', str(tmp_path/'missing-docker'))
    phases.clear()
    fallback = image.gallery_image(tmp_path/'cache', progress=phases.append)
    assert fallback.path == result.path
    assert phases == ['pulling', 'inspecting', 'cached', 'done']
    missing = image.gallery_image(tmp_path/'empty-cache', progress=phases.append)
    assert missing.path is None and missing.error


@pytest.mark.parametrize('reset', [False, True])
def test_config_save_during_resolution_and_reinitialise(tmp_path, monkeypatch, reset):
    from mmw.tui import repositories
    from mmw.tui.sections.sync import SyncSection
    started, release = threading.Event(), threading.Event()
    launches = []
    def resolve(cfg, selection, **kwargs):
        started.set()
        release.wait(30)
        return [object()], []
    async def launch(self, cfg, mirrors, **kwargs):
        launches.append(cfg)
    monkeypatch.setattr(repositories, 'resolve_selection', resolve)
    monkeypatch.setattr(SyncSection, 'start', launch)
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path/'m'),
                        sources=[config.VscodeSource(provider='vscode')])
    config.save(cfg, tmp_path)
    async def scenario():
        app = MmwApp(cfg, tmp_path, initial_section='sync')
        async with app.run_test() as pilot:
            task = asyncio.create_task(app.start_sync([]))
            await until(pilot, started.is_set)
            await app.switch_mode('settings')
            if reset:
                await app.reinitialise()
            else:
                await app.save_settings(max_connections=7)
            release.set()
            await task
            assert launches == ([] if reset else [cfg])
            assert not app._resolving
            assert app._resolve_section.query_one('#btn-sync-stop').disabled
    try:
        asyncio.run(scenario())
    finally:
        release.set()


@pytest.mark.parametrize('cancel', [False, True])
def test_sync_reservation_covers_removal_of_finished_cards(tmp_path, monkeypatch, cancel):
    from mmw.tui import repositories
    from mmw.tui import sync as sync_module
    from mmw.tui.sections.sync import SyncSection
    from test_tui_sync_section import _mirror, _fake_sync_mirror, SRC
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(repositories, 'resolve_selection',
                        lambda *a, **kw: (calls.append(1) or [_mirror()], []))
    monkeypatch.setattr(sync_module, 'sync_mirror', _fake_sync_mirror)
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path/'m'),
                        sources=[config.SourceConfig(url=SRC)])
    async def scenario():
        app = MmwApp(cfg, tmp_path, initial_section='sync')
        async with app.run_test() as pilot:
            entered, release = asyncio.Event(), asyncio.Event()
            async def remove():
                entered.set()
                await release.wait()
            section = app.screen
            assert isinstance(section, SyncSection)
            section.view = SimpleNamespace(running=False, draining=False, remove=remove)
            task = asyncio.create_task(app.start_sync([]))
            await until(pilot, entered.is_set)
            await app.start_sync([])
            assert len(calls) == 1
            if cancel:
                app.cancel_resolution()
            release.set()
            await task
            if cancel:
                assert not section.query(sync_module.SyncView)
            else:
                assert section.query(sync_module.SyncView)
                section.view.stop()
    asyncio.run(scenario())


def test_partial_sync_keeps_extension_resolution_failure_visible(tmp_path, monkeypatch):
    from mmw.tui import repositories
    from mmw.tui import sync as sync_module
    from mmw.tui.sections.sync import SyncSection
    from test_tui_sync_section import _mirror, _fake_sync_mirror, SRC
    error = '⚠ vscode: extensions not resolved — gallery answered HTTP 403'
    monkeypatch.setattr(repositories, 'resolve_selection', lambda *a, **kw: ([_mirror()], [error]))
    monkeypatch.setattr(sync_module, 'sync_mirror', _fake_sync_mirror)
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path/'m'),
                        sources=[config.SourceConfig(url=SRC)])
    async def scenario():
        app = MmwApp(cfg, tmp_path, initial_section='sync')
        async with app.run_test() as pilot:
            await app.start_sync([])
            section = app.screen
            assert isinstance(section, SyncSection)
            section.view.stop()
            await until(pilot, lambda: 'sync is incomplete' in str(section.query_one('#page-status').render()))
            assert section.query_one('#sync-resolve').display
            assert 'extensions not resolved' in str(section.query_one('#sync-resolve').render())
            # A new request clears the preceding failure.
            section.begin_resolution()
            assert not section.resolution_errors
    asyncio.run(scenario())


def test_the_gallery_step_says_what_it_is_doing():
    # Building the catalog read for minutes with nothing on screen.
    from mmw.tui.transfer import TransferView, PUBLISH
    view = TransferView(PUBLISH, 'test')
    view.record('vscode-gallery', 'gallery:reading 120,610 extension descriptions…')
    assert view._phase == 'Gallery · reading 120,610 extension descriptions…'
    assert view._linked == 0 and any('120,610' in line for line in view._log_pending)
    view.record('vscode-gallery', 'gallery:done')
    assert view._phase == ''


def test_a_publish_step_shows_while_it_runs():
    from mmw.tui.transfer import TransferView, PUBLISH
    view = TransferView(PUBLISH, 'test')
    view.record('', 'step:saving the publish record…')
    assert view._phase == 'saving the publish record…'
    assert any('saving the publish record' in line for line in view._log_pending)
