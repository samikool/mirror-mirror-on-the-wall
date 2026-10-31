import asyncio
import threading

import pytest
from textual.widgets import Checkbox, Label, TextArea

from mmw.core import config
from mmw.tui.ui.base import ThemedApp
from test_add_source import _Harness
from waiting import at_step, until
from vscode_fakes import FakeUpstream


@pytest.fixture
def upstream(monkeypatch):
    from mmw.providers.vscode import provider
    fake = FakeUpstream()
    monkeypatch.setattr(provider, '_client', fake.client)
    return fake


def test_lookup_names_and_missing_ids(upstream):
    from mmw.tui.vscode_source import lookup_ids
    src = config.VscodeSource(provider='vscode', extensions={'ids': ['MS-PYTHON.PYTHON', 'missing.id']})
    rows = dict(lookup_ids(src))
    assert rows['ms-python.python']
    assert rows['missing.id'] is None


def test_cancel_edit_preserves_source():
    from mmw.tui.vscode_source import EditVscodePage
    src = config.VscodeSource(provider='vscode', platforms=['linux-x64'])
    async def scenario():
        app = _Harness(lambda: EditVscodePage(src, existing_names=set(), existing_urls=set()))
        async with app.run_test(size=(80, 24)) as pilot:
            await until(pilot, lambda: app.screen.query('#vs-ids'))
            app.screen.query_one('#vs-ids', TextArea).text = 'golang.go'
            await pilot.press('escape')
            await until(pilot, lambda: app.result is None)
        assert src.extensions.ids == []
        assert src.platforms == ['linux-x64']
    asyncio.run(scenario())


def test_invalid_input_sends_no_lookup_request(upstream):
    from mmw.tui.vscode_source import EditVscodePage
    async def scenario():
        app = _Harness(lambda: EditVscodePage(config.VscodeSource(provider='vscode'),
                                             existing_names=set(), existing_urls=set()))
        async with app.run_test() as pilot:
            await until(pilot, lambda: app.screen.query('#vs-ids'))
            app.screen.query_one('#vs-ids', TextArea).text = 'bad/id'
            app.screen.query_one('#btn-vs-lookup').press()
            await pilot.pause()
            assert not upstream.requests
            assert app.result == 'unset'
    asyncio.run(scenario())


@pytest.mark.parametrize('fails', [False, True])
def test_lookup_discards_old_answer_after_input_change(monkeypatch, fails):
    from mmw.tui import vscode_source as vs
    started, release = threading.Event(), threading.Event()
    def lookup(source, **kw):
        started.set()
        release.wait(10)
        if fails:
            raise ValueError('old failure')
        return [('golang.go', 'OLD RESULT')]
    monkeypatch.setattr(vs, 'lookup_ids', lookup)
    async def scenario():
        app = _Harness(lambda: vs.EditVscodePage(config.VscodeSource(provider='vscode'),
                                                existing_names=set(), existing_urls=set()))
        async with app.run_test() as pilot:
            await until(pilot, lambda: app.screen.query('#vs-ids'))
            form = app.screen.query_one(vs.VscodeEditor)
            app.screen.query_one('#vs-ids', TextArea).text = 'golang.go'
            app.screen.query_one('#btn-vs-lookup').press()
            await until(pilot, started.is_set)
            app.screen.query_one('#vs-ids', TextArea).text = 'ms-python.python'
            await pilot.pause()
            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert 'OLD RESULT' not in str(form.query_one('#vs-lookup-status', Label).render())
            assert 'old failure' not in str(form.query_one('#vs-lookup-status', Label).render())
    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_add_all_extensions_saves_without_marketplace_check(upstream):
    from mmw.tui.add_source import AddSourcePage
    async def scenario():
        app = _Harness(lambda: AddSourcePage(entry='vscode'))
        async with app.run_test(size=(100, 32)) as pilot:
            await until(pilot, lambda: app.screen.query('#vs-all'))
            app.screen.query_one('#vs-all', Checkbox).value = True
            app.screen.query_one('#btn-wizard-next').press()
            await until(pilot, lambda: app.result != 'unset')
            assert app.result[0].extensions.all
            assert not upstream.requests
    asyncio.run(scenario())


def test_mixed_setup_confirmation_has_no_apt_field_crash():
    from mmw.tui.wizard_steps import StepConfirm
    class Host(ThemedApp):
        def compose(self):
            yield StepConfirm([
                config.SourceConfig(url='https://deb.example/apt', suites=['stable'],
                                    components=['main'], arches=['amd64']),
                config.VscodeSource(provider='vscode', kinds=['server'], platforms=['linux-x64'])],
                offer_source_packages=False)
    async def scenario():
        app = Host()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.query(StepConfirm)
    asyncio.run(scenario())


def test_duplicate_vscode_policy_replaces_and_retains_network_identity(tmp_path):
    from mmw.tui.repositories import merge_new_sources, replace_source
    old = config.VscodeSource(provider='vscode', platforms=['linux-x64', 'win32-x64'],
                              extensions={'top_n': 200})
    net = config.NetworkConfig(name='lab', select=[config.NetworkSelect(source=old.identity)])
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / 'm'), sources=[old], networks=[net])
    new = config.VscodeSource(provider='vscode', platforms=['linux-x64'], extensions={'ids': ['golang.go']})
    merged, added, updated = merge_new_sources(cfg, [new])
    assert (added, updated) == (0, 1)
    assert merged.sources == [new]
    config.save(replace_source(merged, old.identity, new), tmp_path)
    saved = config.load(tmp_path)
    assert saved.sources[0].extensions.top_n is None
    assert saved.sources[0].platforms == ['linux-x64']
    assert saved.networks[0].select[0].source == old.identity


def test_sources_filter_finds_marketplace_and_extension_ids(tmp_path):
    from mmw.tui.repositories import RepositoryList
    src = config.VscodeSource(provider='vscode', kinds=['server'], extensions={'ids': ['golang.go']})
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / 'm'), sources=[src])
    model = RepositoryList(cfg)
    for needle in ('Marketplace', 'server', 'golang.go'):
        model.filter = needle
        assert model.sorted_sources() == [src]


@pytest.mark.parametrize('size', [(80, 24), (100, 32), (120, 40)])
def test_vscode_edit_actions_visible_and_ids_can_receive_focus(size):
    from mmw.tui.vscode_source import EditVscodePage
    from waiting import settle
    async def scenario():
        src = config.VscodeSource(provider='vscode', extensions={'ids': ['ms-python.python']})
        app = _Harness(lambda: EditVscodePage(src, existing_names=set(), existing_urls=set()))
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            save = app.screen.query_one('#btn-save')
            assert save.region.height == 1
            assert save.region.bottom <= size[1]
            assert save.region.right <= size[0]
            ids = app.screen.query_one('#vs-ids', TextArea)
            ids.focus()
            await pilot.pause()
            assert ids.has_focus
            assert ids.region.width >= 15
            assert ids.region.height >= 3
    asyncio.run(scenario())


@pytest.mark.parametrize('control,value', [('vs-top-n', '0'), ('vs-ids', 'bad/id'), ('vs-name', '')])
def test_invalid_edit_field_receives_focus(control, value):
    from textual.widgets import Input
    from mmw.tui.vscode_source import EditVscodePage
    from waiting import settle
    async def scenario():
        app = _Harness(lambda: EditVscodePage(config.VscodeSource(provider='vscode'),
                                             existing_names=set(), existing_urls=set()))
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            widget = app.screen.query_one('#' + control)
            if isinstance(widget, Input):
                widget.value = value
            else:
                widget.text = value
            await pilot.pause()
            app.screen.query_one('#btn-save').press()
            await until(pilot, lambda: widget.has_focus)
            assert app.result == 'unset'
    asyncio.run(scenario())


@pytest.mark.parametrize('size', [(80, 24), (100, 32), (120, 40)])
def test_dimension_picker_keyboard_returns_focus_and_preserves_empty(size):
    from mmw.tui.vscode_source import EditVscodePage
    from mmw.tui.pick import PickPage
    from waiting import settle
    async def scenario():
        app = _Harness(lambda: EditVscodePage(config.VscodeSource(provider='vscode', platforms=[]),
                                             existing_names=set(), existing_urls=set()))
        async with app.run_test(size=size) as pilot:
            await settle(pilot)
            page = app.screen
            button = page.query_one('#vs-platforms')
            button.focus()
            await pilot.press('enter')
            await until(pilot, lambda: isinstance(app.screen, PickPage))
            assert app.screen.query_one('#pick-list').size.height >= 4
            await pilot.press('escape')
            await until(pilot, lambda: app.screen is page and button.has_focus)
            assert page.editor.form.get_source().platforms == []
    asyncio.run(scenario())


@pytest.mark.parametrize('cancel,fail_save', [(False, False), (True, False), (False, True)])
def test_sources_edit_vscode_persists_or_preserves_inputs(tmp_path, upstream, monkeypatch, cancel, fail_save):
    from textual.widgets import Input
    from mmw.tui.app import MmwApp
    from mmw.tui.vscode_source import EditVscodePage
    from waiting import settle
    original = config.VscodeSource(provider='vscode', kinds=['server'], platforms=['linux-x64'],
                                   extensions={'top_n': 200})
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path/'m'), sources=[original],
                        networks=[config.NetworkConfig(name='lab', select=[
                            config.NetworkSelect(source=original.identity)])])
    config.save(cfg, tmp_path)
    before = config.config_path(tmp_path).read_bytes()
    real_save = config.save
    if fail_save:
        def fail(*args, **kwargs):
            raise PermissionError('read-only test')
        monkeypatch.setattr(config, 'save', fail)
    async def scenario():
        app = MmwApp(cfg, tmp_path, initial_section='repositories')
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            app.screen.query_one('#btn-repo-edit').press()
            await until(pilot, lambda: isinstance(app.screen, EditVscodePage)
                        and app.screen.query('#vs-name'))
            page = app.screen
            page.query_one('#vs-name', Input).value = 'tools'
            page.query_one('#vs-top-n', Input).value = ''
            page.query_one('#vs-ids', TextArea).text = 'golang.go'
            await pilot.pause()
            if cancel:
                await pilot.press('escape')
                await settle(pilot)
                assert config.config_path(tmp_path).read_bytes() == before
                return
            page.query_one('#btn-save').press()
            if fail_save:
                await until(pilot, lambda: 'read-only' in str(page.query_one('#page-status').render()))
                assert app.screen is page
                assert page.query_one('#vs-ids', TextArea).text == 'golang.go'
                assert config.config_path(tmp_path).read_bytes() == before
                monkeypatch.setattr(config, 'save', real_save)
                page.query_one('#btn-save').press()
            await until(pilot, lambda: app.screen is not page)
            await settle(pilot)
            saved = config.load(tmp_path)
            assert saved.sources[0].name == 'tools'
            assert saved.sources[0].extensions.top_n is None
            assert saved.sources[0].extensions.ids == ['golang.go']
            assert saved.networks[0].select[0].source == original.identity
    asyncio.run(scenario())


@pytest.mark.parametrize('mixed', [False, True])
def test_first_setup_adds_vscode_and_writes_policy(tmp_path, upstream, mixed):
    from textual.widgets import Input
    from mmw.tui.app import MmwApp
    from mmw.tui.add_source import AddSourcePage
    from pickers import choose_add_entry
    from waiting import settle
    async def scenario():
        app = MmwApp(None, tmp_path)
        async with app.run_test(size=(100, 32)) as pilot:
            await settle(pilot)
            setup = app.screen
            setup.query_one('#mirror-dir', Input).value = str(tmp_path/'m')
            setup.query_one('#btn-wizard-next').press()
            await until(pilot, lambda: isinstance(app.screen, AddSourcePage))
            await settle(pilot)
            await choose_add_entry(app, pilot, 'vscode')
            add = app.screen
            await until(pilot, lambda: add.query('#vs-ids'))
            from mmw.tui.pick import PickPage
            for field, selected in [('vs-kinds', 'extensions'), ('vs-platforms', 'linux-x64')]:
                add.query_one('#' + field).press()
                await until(pilot, lambda: isinstance(app.screen, PickPage)
                            and app.screen.query('#pick-list'))
                picks = app.screen.query_one('#pick-list')
                picks.deselect_all()
                picks.select(selected)
                app.screen.query_one('#btn-done').press()
                await until(pilot, lambda: app.screen is add)
            add.query_one('#vs-ids', TextArea).text = 'ms-python.python'
            await pilot.pause()
            add.query_one('#btn-wizard-next').press()
            await until(pilot, lambda: app.screen is setup)
            if mixed:
                setup.query_one('#btn-setup-add').press()
                await until(pilot, lambda: isinstance(app.screen, AddSourcePage))
                app.screen.dismiss([config.SourceConfig(url='https://deb.example/apt',
                    suites=['stable'], components=['main'], arches=['amd64'], include_sources=True)])
                await until(pilot, lambda: app.screen is setup)
            setup.query_one('#btn-wizard-next').press()
            await at_step(pilot, setup, 2)
            setup.query_one('#btn-wizard-next').press()
            await until(pilot, lambda: app.current_mode == 'repositories')
            await settle(pilot)
            await app.workers.wait_for_complete()
            await settle(pilot)
            saved = config.load(tmp_path)
            assert saved.sources[0].extensions.ids == ['ms-python.python']
            assert len(saved.sources) == (2 if mixed else 1)
            if mixed:
                assert saved.sources[1].include_sources
    asyncio.run(scenario())


def test_policy_summary_does_not_pull_focus_to_invalid_field():
    from mmw.tui.vscode_source import EditVscodePage
    from textual.widgets import Input
    from waiting import settle
    async def scenario():
        app = _Harness(lambda: EditVscodePage(config.VscodeSource(provider='vscode'),
                                             existing_names=set(), existing_urls=set()))
        async with app.run_test(size=(80, 24)) as pilot:
            await settle(pilot)
            app.screen.query_one('#vs-top-n', Input).value = '0'
            name = app.screen.query_one('#vs-name', Input)
            name.focus()
            await pilot.press('t')
            await pilot.pause()
            assert name.has_focus
    asyncio.run(scenario())
