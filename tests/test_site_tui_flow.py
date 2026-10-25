"""The far-side TUI flow: first launch, placing hostnames, applying, done."""
import asyncio

from mmw.site import recents
from mmw.site.apply import apply_to_set, plan_set_apply
from mmw.site.home import read_root
from mmw.site.mset import LocalRoot, open_set
from mmw.tui.site.app import SiteApp

from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture
from test_site_apply import _bundle
from test_site_set_apply import HOST_A, HOST_B, hosts  # noqa: F401
from waiting import until

quiet = lambda *a: None  # noqa: E731


def _run(mset, check, size=(120, 40)):
    async def go():
        app = SiteApp(mset)
        async with app.run_test(size=size) as pilot:
            await check(app, pilot)
    asyncio.run(go())


def _name(app):
    return type(app.screen).__name__


def test_start_page_with_nothing_opened(tmp_path):
    async def check(app, pilot):
        await until(pilot, lambda: _name(app) == "StartPage")
        assert app.screen.query("#start-apply") and app.screen.query("#start-open")
    _run(None, check)


def test_open_a_mirror_from_the_start_page(hosts, tmp_path):  # noqa: F811
    cfg, net, md = hosts
    r1 = tmp_path / "r1"
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), place={"*": str(r1)}, say=quiet)
    recents.forget(str(r1))

    async def check(app, pilot):
        await until(pilot, lambda: _name(app) == "StartPage")
        app.screen.open_path(str(tmp_path))                        # no mirror there
        await until(pilot, lambda: "no mirror at" in app.screen.status_text())
        app.screen.open_path(str(r1))
        await until(pilot, lambda: _name(app) == "SiteHomePage")
        assert app.mset is not None and str(r1) in recents.known()
    _run(None, check)


def test_first_apply_places_hosts_from_the_tui(hosts, tmp_path):  # noqa: F811
    cfg, net, md = hosts
    b = str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None))
    r1 = tmp_path / "r1"

    async def check(app, pilot):
        await until(pilot, lambda: _name(app) == "StartPage")
        app.screen.apply_path(b)
        await until(pilot, lambda: _name(app) == "PlacePage" and app.screen.query("#btn-place-go"))
        page = app.screen
        await until(pilot, lambda: "choose a root" in page.hosts_text())
        assert page.query_one("#btn-place-go").disabled                # new hostnames have no root yet
        page.add_root(str(r1))
        for h in (HOST_A, HOST_B):
            page.choose_root(h, str(r1))
        await until(pilot, lambda: not page.query_one("#btn-place-go").disabled)
        page.query_one("#btn-place-go").press()
        await until(pilot, lambda: _name(app) == "ApplyRunPage" and app.screen.finished, timeout=30)
        done = str(app.screen.query_one("#run-done").render())
        assert "start nginx in r1" in done and "docker compose up -d" in done
    _run(None, check)
    assert read_root(str(r1)) is not None and str(r1) in recents.known()
    from mmw.site.db import SiteDB
    with SiteDB(read_root(str(r1)).home) as db:
        assert sorted(s.label for s in db.repo_states() if s.serving) == ["a", "b"]


def _applied(hosts, tmp_path):  # noqa: F811
    cfg, net, md = hosts
    r1 = tmp_path / "r1"
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), place={"*": str(r1)}, say=quiet)
    return cfg, net, md, r1


def _place_page(mset, src, check):
    async def go():
        from mmw.tui.site.place import PlacePage
        app = SiteApp(mset)
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: _name(app) in ("SiteHomePage", "StartPage"))
            app.push_screen(PlacePage(plan_set_apply(mset, src), src, mset))
            await until(pilot, lambda: _name(app) == "PlacePage" and app.screen.query("#btn-place-go"))
            await check(app, pilot, app.screen)
    asyncio.run(go())


def test_known_hosts_route_themselves(hosts, tmp_path):  # noqa: F811
    from test_bundle_mmw import _change_b
    from test_snapshot_repo_versions import _change_a
    cfg, net, md, r1 = _applied(hosts, tmp_path)
    _change_a(md)
    _change_b(md)
    b = str(_bundle(cfg, net, md, tmp_path / "2"))

    async def check(app, pilot, page):
        await until(pilot, lambda: not page.query_one("#btn-place-go").disabled)
        assert page.targets == {HOST_A: "r1", HOST_B: "r1"} and "known" in page.hosts_text()
    _place_page(open_set(), b, check)


def test_needs_follow_the_placement(hosts, tmp_path, monkeypatch):  # noqa: F811
    cfg, net, md = hosts
    b = str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None))
    small, big = tmp_path / "small", tmp_path / "big"
    small.mkdir()
    big.mkdir()
    from mmw.core import disk_io
    real = disk_io.ops.disk_usage
    monkeypatch.setattr(disk_io.ops, "disk_usage",
                        lambda p: real(p)._replace(free=10) if str(p).startswith(str(small)) else real(p))

    async def check(app, pilot, page):
        page.add_root(str(small))
        page.add_root(str(big))
        for h in (HOST_A, HOST_B):
            page.choose_root(h, str(small))
        await until(pilot, lambda: "✗" in page.roots_text())
        assert page.query_one("#btn-place-go").disabled
        for h in (HOST_A, HOST_B):
            page.choose_root(h, str(big))
        await until(pilot, lambda: "✗" not in page.roots_text())
        assert not page.query_one("#btn-place-go").disabled
    _place_page(None, b, check)


def test_adding_another_mirrors_root_is_refused(hosts, tmp_path):  # noqa: F811
    cfg, net, md, r1 = _applied(hosts, tmp_path)
    from mmw.site.home import init_root
    other = tmp_path / "other"
    init_root(other, name="other", set_id="elsewhere", network="elm")
    b = str(_bundle(cfg, net, md, tmp_path / "x", from_ref=None))

    async def check(app, pilot, page):
        page.add_root(str(other))
        await until(pilot, lambda: "elm" in page.status_text())
        assert str(other) not in page.roots_text()
    _place_page(open_set(), b, check)


# ── the apply screen, one repository at a time ───────────────────────────────

def test_rows_move_one_repository_at_a_time(tmp_path):
    from mmw.tui.site.apply_flow import ApplyChoice, ApplyRunPage
    from mmw.tui.ui.base import ThemedApp

    class Quiet(ApplyRunPage):
        def _run(self):
            pass
    choice = ApplyChoice("b.tar", frozenset({"ka", "kb"}), 300, (("ka", "a"), ("kb", "b")),
                         sizes=(("ka", 100), ("kb", 200)))

    async def go():
        app = ThemedApp()
        async with app.run_test(size=(120, 40)) as pilot:
            page = Quiet(choice)
            app.push_screen(page)
            await until(pilot, lambda: page.query("#run-repos"))
            text = lambda: str(page.query_one("#run-repos").render())  # noqa: E731
            assert text().count("waiting") == 2
            page._on_progress("repo", {"repo": "ka", "label": "a"})
            page._on_progress("receive", {"bytes": 50, "name": "example.com/a/a.deb"})
            assert text().count("▶") == 1 and "50%" in text()
            assert "a.deb" in str(page.query_one("#run-file").render())
            page._on_progress("repo", {"repo": "kb", "label": "b"})
            assert text().count("▶") == 1 and "received" in text()
            page._on_progress("switch", {"repo": "ka", "label": "a"})
            page._on_progress("switch", {"repo": "kb", "label": "b"})
            page._on_progress("done", {})
            assert text().count("✓") == 2 and "▶" not in text()
    asyncio.run(go())


def test_stop_before_switch_changes_nothing(hosts, tmp_path, monkeypatch):  # noqa: F811
    import time

    from test_site_apply import _tree
    from test_bundle_mmw import _change_b
    from test_snapshot_repo_versions import _change_a
    from mmw.site import apply as apply_mod
    cfg, net, md, r1 = _applied(hosts, tmp_path)
    _change_a(md)
    _change_b(md)
    b = str(_bundle(cfg, net, md, tmp_path / "2"))
    before = _tree(r1)
    real = apply_mod.members_of

    def slow(*a, **kw):
        time.sleep(1.0)
        return real(*a, **kw)
    monkeypatch.setattr(apply_mod, "members_of", slow)

    async def check(app, pilot):
        from mmw.tui.site.apply_flow import ApplyChoice, ApplyRunPage
        await until(pilot, lambda: _name(app) == "SiteHomePage")
        page = ApplyRunPage(ApplyChoice(b, frozenset(), 1, ()))
        app.push_screen(page)
        await until(pilot, lambda: page.query("#btn-run-stop"))
        page.query_one("#btn-run-stop").press()
        await until(pilot, lambda: page.finished, timeout=20)
        assert "stopped" in str(page.query_one("#run-done").render())
        assert "stored" in str(page.query_one("#run-done").render())
    _run(open_set(), check)
    assert _tree(r1) == before
