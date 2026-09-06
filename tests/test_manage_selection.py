"""Selecting repositories in the manage list, and syncing only those.

The list used to key everything on **expanded mirror names**, which are a guess
until the repository has been listed: `arches = []` means "all", and for an apt
source `components = []` expands to *nothing at all*. So a repository mirroring
everything showed "(0)", could not be ticked (an empty name set is a subset of
any selection, so the toggle was a no-op), and a selection that survived to the
sync launch was silently widened back to everything.
"""
import asyncio

from textual.widgets import ListItem, ListView

from mmw.cli import _resolved_for_sync, _split_selection
from mmw.core import config
from mmw.tui.manage import ManageApp


def _cfg(tmp_path, *sources):
    cfg = config.Config(
        settings=config.Settings(mirror_dir=tmp_path / "mirror", max_connections=4),
        sources=list(sources),
    )
    return cfg, tmp_path


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


async def _highlight(pilot, item_id: str) -> None:
    lv = pilot.app.query_one("#manage-list", ListView)
    lv.focus()
    for i, item in enumerate(lv.query(ListItem)):
        if item.id == item_id:
            lv.index = i
            await pilot.pause()
            return
    raise AssertionError(f"{item_id} not in the list")


def _run(scenario):
    asyncio.run(scenario())


# ── the list renders an unexpandable repository honestly ─────────────────────

def test_a_repository_mirroring_everything_does_not_render_as_zero(tmp_path):
    cfg_obj, cfg_dir = _cfg(tmp_path, _open_source())

    async def scenario():
        app = ManageApp(cfg_obj, cfg_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            label = app._source_label(cfg_obj.sources[0])

        assert "(0)" not in label
        assert "resolved at sync" in label

    _run(scenario)


def test_a_repository_with_stated_dimensions_still_shows_its_count(tmp_path):
    cfg_obj, cfg_dir = _cfg(tmp_path, _closed_source())

    async def scenario():
        app = ManageApp(cfg_obj, cfg_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert "(1)" in app._source_label(cfg_obj.sources[0])

    _run(scenario)


# ── selection ────────────────────────────────────────────────────────────────

def test_an_unexpanded_repository_can_be_selected(tmp_path):
    """The regression: an empty name set is a subset of any selection, so the
    toggle removed nothing and added nothing — the row never ticked."""
    cfg_obj, cfg_dir = _cfg(tmp_path, _open_source())

    async def scenario():
        app = ManageApp(cfg_obj, cfg_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _highlight(pilot, app._source_safe_id("spark"))
            app.action_toggle_selection()
            await pilot.pause()

            assert app._selected_sources == {cfg_obj.sources[0].identity}
            assert "✓" in app._source_label(cfg_obj.sources[0])

            app.action_toggle_selection()
            await pilot.pause()
            assert app._selected_sources == set()

    _run(scenario)


def test_selecting_some_repositories_syncs_only_those(tmp_path):
    cfg_obj, cfg_dir = _cfg(
        tmp_path, _open_source("spark"), _open_source("baseos", "baseos.example.com"),
        _closed_source(),
    )

    async def scenario():
        app = ManageApp(cfg_obj, cfg_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _highlight(pilot, app._source_safe_id("spark"))
            app.action_toggle_selection()
            await _highlight(pilot, app._source_safe_id("pinned"))
            app.action_toggle_selection()
            await pilot.pause()
            app.action_launch_sync()
            await pilot.pause()
            await pilot.pause()

        kind, selection = app.return_value
        sources, _ = _split_selection(selection)
        assert kind == "sync"
        assert sources == {cfg_obj.sources[0].identity, cfg_obj.sources[2].identity}

    _run(scenario)


def test_select_all_covers_repositories_that_expand_to_nothing(tmp_path):
    cfg_obj, cfg_dir = _cfg(tmp_path, _open_source(), _closed_source())

    async def scenario():
        app = ManageApp(cfg_obj, cfg_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_select_all()
            await pilot.pause()
            await pilot.pause()

            assert app._selected_sources == {s.identity for s in cfg_obj.sources}

    _run(scenario)


# ── launching a sync ─────────────────────────────────────────────────────────

def test_sync_is_launchable_when_sources_expand_to_nothing(tmp_path):
    """The reported bug: "no mirrors configured" with seven repositories added.
    The gate asked the offline expansion, which is empty for `components = []`."""
    cfg_obj, cfg_dir = _cfg(tmp_path, _open_source())

    async def scenario():
        app = ManageApp(cfg_obj, cfg_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_launch_sync()
            await pilot.pause()
            await pilot.pause()

        assert app.return_value is not None, "sync must be reachable"
        assert app.return_value[0] == "sync"

    _run(scenario)


def test_sync_with_no_sources_at_all_is_refused(tmp_path):
    cfg_obj, cfg_dir = _cfg(tmp_path)

    async def scenario():
        app = ManageApp(cfg_obj, cfg_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app.action_launch_sync()
            await pilot.pause()
            await pilot.pause()

        assert app.return_value is None

    _run(scenario)


def test_adding_sources_refreshes_what_the_list_offers(tmp_path):
    """`_available` was computed once at startup, so adding to an empty config
    left the sync gate looking at the answer from before the add."""
    cfg_obj, cfg_dir = _cfg(tmp_path)

    async def scenario():
        app = ManageApp(cfg_obj, cfg_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app._available == []

            cfg_obj.sources = [_closed_source()]
            app._refresh_available()
            await pilot.pause()

            assert [m.name for m in app._available] == ["pinned-noble-main-arm64"]

    _run(scenario)


# ── resolution keeps the selection meaning what it said ──────────────────────

def _mirror(name, source):
    return config.MirrorConfig(name=name, provider="apt", source=source,
                               source_url=source, arch="arm64",
                               suite="noble", component="main")


def test_resolution_narrows_by_repository_not_by_guessed_name(monkeypatch):
    """`arches = []` expands to one invented `amd64` offline and to the real
    arches upstream, so a name chosen in the list need not exist afterwards.
    The repository identity does."""
    import mmw.commands.mirror as mirror_cmd

    resolved = [_mirror("spark-noble-main-arm64", "https://a.example.com/spark"),
                _mirror("other-noble-main-arm64", "https://b.example.com/other")]
    monkeypatch.setattr(mirror_cmd, "resolve_and_report", lambda cfg: (resolved, []))

    got = _resolved_for_sync(None, (["https://a.example.com/spark"], []))
    assert [m.name for m in got] == ["spark-noble-main-arm64"]


def test_a_selection_that_resolves_to_nothing_never_widens_to_everything(monkeypatch):
    """Syncing terabytes nobody asked for is far worse than syncing nothing."""
    import mmw.commands.mirror as mirror_cmd

    resolved = [_mirror("other-noble-main-arm64", "https://b.example.com/other")]
    monkeypatch.setattr(mirror_cmd, "resolve_and_report", lambda cfg: (resolved, []))

    assert _resolved_for_sync(None, (["https://gone.example.com/x"], [])) is None


def test_no_selection_still_means_everything(monkeypatch):
    import mmw.commands.mirror as mirror_cmd

    resolved = [_mirror("a-noble-main-arm64", "https://a.example.com/a"),
                _mirror("b-noble-main-arm64", "https://b.example.com/b")]
    monkeypatch.setattr(mirror_cmd, "resolve_and_report", lambda cfg: (resolved, []))

    assert len(_resolved_for_sync(None, ((), ()))) == 2


def test_a_plain_list_of_names_still_works(monkeypatch):
    """Older callers hand back just names."""
    import mmw.commands.mirror as mirror_cmd

    resolved = [_mirror("a-noble-main-arm64", "https://a.example.com/a"),
                _mirror("b-noble-main-arm64", "https://b.example.com/b")]
    monkeypatch.setattr(mirror_cmd, "resolve_and_report", lambda cfg: (resolved, []))

    got = _resolved_for_sync(None, ["b-noble-main-arm64"])
    assert [m.name for m in got] == ["b-noble-main-arm64"]


# ── list geometry ────────────────────────────────────────────────────────────

def test_every_list_row_is_exactly_one_row_tall(tmp_path):
    """`border-top` adds a line to the box and `margin-top` adds another, so a
    source row used to occupy three rows for one line of text: seven
    repositories filled the whole list with whitespace, and the highlight bar
    covered the gap above the row it was on rather than the row itself."""
    from textual.widgets import ListItem as TItem

    cfg_obj, cfg_dir = _cfg(
        tmp_path, _closed_source("a", "one.example.com"),
        _closed_source("b", "one.example.com"), _closed_source("c", "two.example.com"),
    )

    async def scenario():
        app = ManageApp(cfg_obj, cfg_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.pause()
            heights = {it.id: it.region.height for it in app.query("#manage-list").first().query(TItem)}

        assert heights, "the list rendered nothing"
        assert set(heights.values()) == {1}, f"rows taller than one line: {heights}"

    _run(scenario)


def test_the_list_fits_its_repositories_without_scrolling(tmp_path):
    """Three repositories across two hosts is 5 rows of content plus group
    spacing — it must not need a 35-row list."""
    cfg_obj, cfg_dir = _cfg(
        tmp_path, _closed_source("a", "one.example.com"),
        _closed_source("b", "one.example.com"), _closed_source("c", "two.example.com"),
    )

    async def scenario():
        app = ManageApp(cfg_obj, cfg_dir)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.pause()
            lv = app.query_one("#manage-list")
            rows = [it.region.y for it in lv.query("ListItem")]

        assert max(rows) - min(rows) <= 7, "rows are spread far wider than their content"

    _run(scenario)


def test_the_select_binding_is_advertised():
    """Multi-select is invisible otherwise — nothing else on screen hints that
    the list can be ticked."""
    space = [b for b in ManageApp.BINDINGS if getattr(b, "key", None) == "space"]
    assert space and space[0].show is True
