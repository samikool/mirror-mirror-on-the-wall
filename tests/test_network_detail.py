"""The network detail pane: theme markup, escaping, and no stale results."""
import asyncio
from pathlib import Path

from mmw.core import config
from mmw.tui.sections.network_detail import NetworkDetail
from mmw.tui.ui.base import ThemedApp
from waiting import settle


def _cfg():
    return config.Config(settings=config.Settings(mirror_dir=Path("/nonexistent/pool")))


def _run(check, monkeypatch):
    # The real loader would race the results these tests hand in.
    monkeypatch.setattr(NetworkDetail, "_load", lambda self, gen, net, cfg: None)

    class _App(ThemedApp):
        def compose(self):
            yield NetworkDetail()

    async def go():
        app = _App()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await check(app, pilot)
    asyncio.run(go())


def _text(app):
    return "\n".join(str(w.render()) for w in app.query_one(NetworkDetail).query("Label"))


def test_a_result_for_a_previous_network_is_dropped(monkeypatch):
    async def check(app, pilot):
        pane = app.query_one(NetworkDetail)
        cfg = _cfg()
        pane.show(config.NetworkConfig(name="old", select=[]), cfg)
        stale_gen = pane._detail_gen
        pane.show(config.NetworkConfig(name="new", select=[]), cfg)
        pane._on_loaded(stale_gen, ["old-mirror"], (5, 50, None, []))
        await settle(pilot)
        assert "old-mirror" not in _text(app)
        pane._on_loaded(pane._detail_gen, ["new-mirror"], None)
        await settle(pilot)
        assert "new-mirror" in _text(app)
    _run(check, monkeypatch)


def test_names_and_paths_render_literally(monkeypatch):
    async def check(app, pilot):
        pane = app.query_one(NetworkDetail)
        net = config.NetworkConfig(name="lab", root=Path("/srv/a[b]c"), select=[])
        pane.show(net, _cfg())
        pane._on_loaded(pane._detail_gen, ["m[i]x"], None)
        await settle(pilot)
        text = _text(app)
        assert "/srv/a[b]c" in text and "m[i]x" in text
    _run(check, monkeypatch)


def test_a_failed_load_says_so_instead_of_loading_forever(monkeypatch):
    from mmw.tui.sections import network_detail as nd

    def boom(*a, **k):
        raise RuntimeError("pool unreadable")
    monkeypatch.setattr(nd, "network_mirrors", boom)

    class _App(ThemedApp):
        def compose(self):
            yield NetworkDetail()

    async def go():
        app = _App()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.query_one(NetworkDetail).show(
                config.NetworkConfig(name="lab", root=Path("/srv/lab"), select=[]), _cfg())
            await settle(pilot)
            await app.workers.wait_for_complete()
            await settle(pilot)
            assert "pool unreadable" in _text(app)
    asyncio.run(go())
