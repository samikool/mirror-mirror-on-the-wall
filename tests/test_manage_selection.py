"""Selecting repositories in the Repositories list, and syncing only those.

The list used to key everything on **expanded mirror names**, which are a guess
until the repository has been listed: `arches = []` means "all", and for an apt
source `components = []` expands to *nothing at all*. So a repository mirroring
everything showed "(0)", could not be ticked (an empty name set is a subset of
any selection, so the toggle was a no-op), and a selection that survived to the
sync launch was silently widened back to everything.
"""
import asyncio

from textual.widgets import ListView

from mmw.core import config
from mmw.tui import repositories as R
from mmw.tui.app import MmwApp
from mmw.tui.sections.repositories import RepositoriesPage


def _cfg(tmp_path, *sources):
    return config.Config(
        settings=config.Settings(mirror_dir=tmp_path / "mirror", max_connections=4),
        sources=list(sources),
    )


def _open_source(name="spark", host="repo.example.com"):
    """A repository mirroring everything — the shape that broke the list."""
    return config.SourceConfig(
        name=name, provider="apt", url=f"https://{host}/{name}",
        suites=["noble"], components=[], arches=[],
    )


def _closed_source(name="pinned", host="pinned.example.com"):
    return config.SourceConfig(
        name=name, provider="apt", url=f"https://{host}/{name}",
        suites=["noble"], components=["main"], arches=["arm64"],
    )


async def _settle(pilot, n=6):
    for _ in range(n):
        await pilot.pause()


def _run(tmp_path, cfg, check):
    """Open Repositories; `app.sync_requests` records what `s` asks to sync."""
    config.save(cfg, tmp_path)

    async def go():
        app = MmwApp(cfg, tmp_path, initial_section="repositories")
        app.sync_requests = []
        app.notes = []
        app.notify = lambda m, **_: app.notes.append(m)
        real_start = app.start_sync

        async def recording_start(selection):
            app.sync_requests.append(selection)
            if not app.cfg.sources:
                await real_start(selection)       # the refusal is the app's
        app.start_sync = recording_start
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            await app.workers.wait_for_complete()
            await _settle(pilot)
            await check(app, pilot)
    asyncio.run(go())


def _labels(app):
    return [str(item.query_one("Label").render())
            for item in app.screen.query_one("#repo-list", ListView).children]


async def _highlight(app, pilot, name: str) -> None:
    lst = app.screen.query_one("#repo-list", ListView)
    lst.focus()
    for i, item in enumerate(lst.children):
        if item.id == f"source-{name}" or name in str(item.query_one("Label").render()):
            if "/" in str(item.query_one("Label").render()):
                lst.index = i
                await _settle(pilot)
                return
    raise AssertionError(f"{name} not in the list: {_labels(app)}")


# ── the list renders an unexpandable repository honestly ─────────────────────

def test_a_repository_mirroring_everything_does_not_render_as_zero(tmp_path):
    async def check(app, pilot):
        row = next(lbl for lbl in _labels(app) if "/spark" in lbl)
        assert "(0)" not in row
        assert "resolved at sync" in row
    _run(tmp_path, _cfg(tmp_path, _open_source()), check)


def test_a_repository_with_stated_dimensions_still_shows_its_count(tmp_path):
    async def check(app, pilot):
        assert "(1)" in next(lbl for lbl in _labels(app) if "/pinned" in lbl)
    _run(tmp_path, _cfg(tmp_path, _closed_source()), check)


# ── selection ────────────────────────────────────────────────────────────────

def test_an_unexpanded_repository_can_be_selected(tmp_path):
    """The regression: an empty name set is a subset of any selection, so the
    toggle removed nothing and added nothing — the row never ticked."""
    cfg = _cfg(tmp_path, _open_source())

    async def check(app, pilot):
        await _highlight(app, pilot, "spark")
        await pilot.press("space")
        await _settle(pilot)
        assert app.screen.model.selected_sources == {cfg.sources[0].identity}
        assert "✓" in next(lbl for lbl in _labels(app) if "spark" in lbl)
        await pilot.press("space")
        await _settle(pilot)
        assert app.screen.model.selected_sources == set()
    _run(tmp_path, cfg, check)


def test_selecting_some_repositories_syncs_only_those(tmp_path):
    cfg = _cfg(tmp_path, _open_source("spark"), _open_source("baseos", "baseos.example.com"),
               _closed_source())

    async def check(app, pilot):
        await _highlight(app, pilot, "spark")
        await pilot.press("space")
        await _highlight(app, pilot, "pinned")
        await pilot.press("space")
        await _settle(pilot)
        await pilot.press("s")
        await _settle(pilot)
        sources, _ = R.split_selection(app.sync_requests[-1])
        assert sources == {cfg.sources[0].identity, cfg.sources[2].identity}
    _run(tmp_path, cfg, check)


def test_select_all_covers_repositories_that_expand_to_nothing(tmp_path):
    cfg = _cfg(tmp_path, _open_source(), _closed_source())

    async def check(app, pilot):
        app.screen.query_one("#repo-list", ListView).focus()
        await pilot.press("ctrl+a")
        await _settle(pilot)
        assert app.screen.model.selected_sources == {s.identity for s in cfg.sources}
    _run(tmp_path, cfg, check)


def test_the_tick_binding_is_advertised():
    """Multi-select is invisible otherwise — nothing else on screen hints that
    the list can be ticked."""
    space = [b for b in RepositoriesPage.BINDINGS if getattr(b, "key", None) == "space"]
    assert space and space[0].show is True


# ── launching a sync ─────────────────────────────────────────────────────────

def test_sync_is_launchable_when_sources_expand_to_nothing(tmp_path):
    """The reported bug: "no mirrors configured" with seven repositories added.
    The gate asked the offline expansion, which is empty for `components = []`."""
    async def check(app, pilot):
        app.screen.query_one("#repo-list", ListView).focus()
        await pilot.press("s")
        await _settle(pilot)
        assert app.sync_requests, "sync must be reachable"
    _run(tmp_path, _cfg(tmp_path, _open_source()), check)


def test_sync_with_no_sources_at_all_is_refused(tmp_path):
    async def check(app, pilot):
        await app.start_sync(((), ()))
        await _settle(pilot)
        assert app.active_sync is None
        assert any("No repositories configured" in n for n in app.notes)
    _run(tmp_path, _cfg(tmp_path), check)


def test_adding_sources_refreshes_what_the_list_offers(tmp_path):
    """The list's mirrors were computed once at startup, so adding to an empty
    config left the sync gate looking at the answer from before the add."""
    async def check(app, pilot):
        assert app.screen.model.available == []
        await app.save_config(lambda c: c.model_copy(update={"sources": [_closed_source()]}))
        await app.screen._after_write()       # what the add flow runs after saving
        await _settle(pilot)
        await app.workers.wait_for_complete()
        await _settle(pilot)
        assert [m.name for m in app.screen.model.available] == ["pinned-noble-main-arm64"]
        assert any("pinned" in lbl for lbl in _labels(app))
    _run(tmp_path, _cfg(tmp_path), check)


# ── resolution keeps the selection meaning what it said ──────────────────────

def _mirror(name, source):
    return config.MirrorConfig(name=name, provider="apt", source=source,
                               source_url=source, arch="arm64",
                               suite="noble", component="main")


def test_resolution_narrows_by_repository_not_by_guessed_name():
    """`arches = []` expands to one invented `amd64` offline and to the real
    arches upstream, so a name chosen in the list need not exist afterwards.
    The repository identity does."""
    resolved = [_mirror("spark-noble-main-arm64", "https://a.example.com/spark"),
                _mirror("other-noble-main-arm64", "https://b.example.com/other")]
    got, _, _ = R.narrow_to_selection(resolved, [], (["https://a.example.com/spark"], []))
    assert [m.name for m in got] == ["spark-noble-main-arm64"]


def test_a_selection_that_resolves_to_nothing_never_widens_to_everything():
    """Syncing terabytes nobody asked for is far worse than syncing nothing."""
    resolved = [_mirror("other-noble-main-arm64", "https://b.example.com/other")]
    got, problem, _ = R.narrow_to_selection(resolved, [], (["https://gone.example.com/x"], []))
    assert got == [] and problem


def test_no_selection_still_means_everything():
    resolved = [_mirror("a-noble-main-arm64", "https://a.example.com/a"),
                _mirror("b-noble-main-arm64", "https://b.example.com/b")]
    assert len(R.narrow_to_selection(resolved, [], ((), ()))[0]) == 2


def test_a_plain_list_of_names_still_works():
    """Older callers hand back just names."""
    resolved = [_mirror("a-noble-main-arm64", "https://a.example.com/a"),
                _mirror("b-noble-main-arm64", "https://b.example.com/b")]
    got, _, _ = R.narrow_to_selection(resolved, [], ["b-noble-main-arm64"])
    assert [m.name for m in got] == ["b-noble-main-arm64"]


# ── list geometry ────────────────────────────────────────────────────────────

def test_the_list_fits_its_repositories_without_scrolling(tmp_path):
    """Three repositories across two hosts is 5 rows of content — it must not
    need a 35-row list."""
    cfg = _cfg(tmp_path, _closed_source("a", "one.example.com"),
               _closed_source("b", "one.example.com"), _closed_source("c", "two.example.com"))

    async def check(app, pilot):
        rows = [it.region.y for it in app.screen.query_one("#repo-list").children]
        assert max(rows) - min(rows) <= 7, "rows are spread far wider than their content"
    _run(tmp_path, cfg, check)
