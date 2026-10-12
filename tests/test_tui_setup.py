"""First-run setup and re-initialise, inside MmwApp."""
import asyncio

from textual.widgets import Button, Input

from mmw.core import config
from mmw.tui.add_source import AddSourcePage
from mmw.tui.app import MmwApp
from mmw.tui.sections.setup import SetupPage
from mmw.tui.ui.dialog import ConfirmDialog
from mmw.tui.ui.sidebar import Sidebar

SRC = "https://deb.example.com/debian"


def _source():
    return config.SourceConfig(url=SRC, suites=["bookworm"], components=["main"], arches=["amd64"])


async def _settle(pilot, n=6):
    for _ in range(n):
        await pilot.pause()


def _run(tmp_path, check, cfg=None, **kw):
    async def go():
        app = MmwApp(cfg, tmp_path, **kw)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            await check(app, pilot)
    asyncio.run(go())


def _setup_page(app):
    return next(s for s in app.screen_stack if isinstance(s, SetupPage))


def test_no_config_opens_setup_without_a_sidebar(tmp_path):
    async def check(app, pilot):
        assert isinstance(app.screen, SetupPage)
        assert not app.screen.query(Sidebar)
    _run(tmp_path, check)


def test_setup_writes_the_config_and_continues_into_repositories(tmp_path):
    async def check(app, pilot):
        page = app.screen
        page.query_one("#mirror-dir", Input).value = str(tmp_path / "pool")
        page.query_one("#btn-wizard-next", Button).press()          # → Repositories
        await _settle(pilot)
        assert isinstance(app.screen, AddSourcePage)            # opened for you
        app.screen.dismiss([_source()])
        await _settle(pilot)
        page.query_one("#btn-wizard-next", Button).press()          # → Confirm
        await _settle(pilot)
        page.query_one("#btn-wizard-next", Button).press()          # write
        await _settle(pilot, 10)
        await app.workers.wait_for_complete()
        await _settle(pilot)
        saved = config.load(tmp_path)
        assert [s.url for s in saved.sources] == [SRC]
        assert saved.settings.mirror_dir == (tmp_path / "pool").resolve()
        assert app.current_mode == "repositories" and app.screen.query(Sidebar)
    _run(tmp_path, check)


def test_esc_in_the_first_add_asks_before_quitting_setup(tmp_path):
    async def check(app, pilot):
        app.screen.query_one("#btn-wizard-next", Button).press()
        await _settle(pilot)
        assert isinstance(app.screen, AddSourcePage)
        await pilot.press("escape")
        await _settle(pilot)
        assert isinstance(app.screen, ConfirmDialog)
        await pilot.press("escape")                                  # go back
        await _settle(pilot)
        assert app.is_running and isinstance(app.screen, SetupPage)
        assert app.screen.step == 1
    _run(tmp_path, check)


def test_reinitialise_from_settings_deletes_the_config_and_runs_setup(tmp_path):
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "pool"),
                        sources=[_source()])
    config.save(cfg, tmp_path)

    async def check(app, pilot):
        app.screen.query_one("#btn-settings-reinit", Button).press()
        await _settle(pilot)
        assert isinstance(app.screen, ConfirmDialog)
        app.screen.query_one("#confirm").press()
        await _settle(pilot, 10)
        await app.workers.wait_for_complete()
        await _settle(pilot)
        assert not config.config_path(tmp_path).exists()
        assert isinstance(app.screen, SetupPage) and app.cfg is None
    _run(tmp_path, check, cfg=cfg, initial_section="settings")


# ── review fixes ─────────────────────────────────────────────────────────────

def _cfg_at(tmp_path):
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "bigpool",
                                                 max_connections=24),
                        sources=[_source()])
    config.save(cfg, tmp_path)
    return cfg


def test_reinitialise_proposes_the_pool_already_in_use(tmp_path):
    """Re-init offered `./mirror` and cpu_count: pressing Next through Setup
    pointed mmw at an empty pool and re-downloaded terabytes."""
    async def check(app, pilot):
        await app.reinitialise()
        await _settle(pilot)
        page = app.screen
        assert isinstance(page, SetupPage)
        assert page.query_one("#mirror-dir", Input).value == str(tmp_path / "bigpool")
        assert page.query_one("#max-connections", Input).value == "24"
    _run(tmp_path, check, cfg=_cfg_at(tmp_path), initial_section="settings")


def test_setup_keeps_each_repositorys_deb_src_choice(tmp_path):
    """Setup's own Confirm defaulted a global deb-src box to any() and forced
    it onto every repository — B got source packages it was added without."""
    a = config.SourceConfig(url=SRC, suites=["bookworm"], components=["main"],
                            arches=["amd64"], include_sources=True)
    b = config.SourceConfig(url="https://ppa.example.org/ubuntu", suites=["noble"],
                            components=["main"], arches=["amd64"], include_sources=False)

    async def check(app, pilot):
        page = app.screen
        page.query_one("#mirror-dir", Input).value = str(tmp_path / "pool")
        page.query_one("#btn-wizard-next", Button).press()
        await _settle(pilot)
        app.screen.dismiss([a, b])
        await _settle(pilot)
        page.query_one("#btn-wizard-next", Button).press()          # → Confirm
        await _settle(pilot)
        assert not page.query("#src-switch")                        # decided per add
        page.query_one("#btn-wizard-next", Button).press()          # write
        await _settle(pilot, 10)
        await app.workers.wait_for_complete()
        saved = {s.url: s.include_sources for s in config.load(tmp_path).sources}
        assert saved == {SRC: True, "https://ppa.example.org/ubuntu": False}
    _run(tmp_path, check)


def test_reinitialise_waits_for_an_action_in_flight(tmp_path):
    """A prune or a resolving sync on another section must not be cut off by
    re-init tearing that section down."""
    async def check(app, pilot):
        await app.switch_mode("repositories")
        await _settle(pilot)
        repos = app.screen
        release = asyncio.Event()

        async def slow():
            await release.wait()
        repos.start_flow(slow)
        await app.switch_mode("settings")
        await _settle(pilot)
        notes = []
        app.notify = lambda m, **_: notes.append(m)
        await app.reinitialise()
        assert config.config_path(tmp_path).exists() and app.cfg is not None
        assert any("finish" in n.lower() for n in notes), notes
        release.set()
        await _settle(pilot)
    _run(tmp_path, check, cfg=_cfg_at(tmp_path), initial_section="settings")


def test_a_sync_resolving_when_setup_starts_does_not_start(tmp_path, monkeypatch):
    from mmw.tui import repositories as R
    release = None

    async def check(app, pilot):
        nonlocal release
        import threading
        release = threading.Event()

        def slow_resolve(cfg, sel):
            release.wait(5)
            return [config.MirrorConfig(name="debian-bookworm-main-amd64", provider="apt",
                                        source=SRC, source_url=SRC, arch="amd64",
                                        suite="bookworm", component="main")], []
        monkeypatch.setattr(R, "resolve_selection", slow_resolve)
        task = asyncio.ensure_future(app.start_sync(((), ())))
        await _settle(pilot)
        app.cfg = None                     # what reinitialise does meanwhile
        release.set()
        await task                         # must not touch the Sync section with no config
        assert app.active_sync is None
    _run(tmp_path, check, cfg=_cfg_at(tmp_path), initial_section="settings")


def test_a_failed_reinitialise_is_reported_not_fatal(tmp_path, monkeypatch):
    def boom(cfg_dir):
        raise PermissionError("read-only")
    monkeypatch.setattr(config, "delete", boom)

    async def check(app, pilot):
        notes = []
        app.notify = lambda m, **_: notes.append(m)
        app.screen.query_one("#btn-settings-reinit", Button).press()
        await _settle(pilot)
        app.screen.query_one("#confirm").press()
        await _settle(pilot, 10)
        assert app.is_running and app.cfg is not None
        assert any("read-only" in n for n in notes), notes
    _run(tmp_path, check, cfg=_cfg_at(tmp_path), initial_section="settings")


def test_one_url_added_as_both_shapes_is_a_notice_not_a_crash(tmp_path):
    async def check(app, pilot):
        page = app.screen
        page.query_one("#mirror-dir", Input).value = str(tmp_path / "pool")
        page.query_one("#btn-wizard-next", Button).press()
        await _settle(pilot)
        app.screen.dismiss([_source()])
        await _settle(pilot)
        page.query_one("#btn-setup-add", Button).press()
        await _settle(pilot)
        app.screen.dismiss([config.SourceConfig(url=SRC, provider="apt-flat")])
        await _settle(pilot)
        assert app.is_running and isinstance(app.screen, SetupPage)
        assert [s.provider for s in _setup_page(app)._planned] == ["apt"]
    _run(tmp_path, check)
