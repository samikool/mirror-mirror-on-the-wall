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


# ── review findings ──────────────────────────────────────────────────────────

def test_keys_place_a_first_bundle(hosts, tmp_path):  # noqa: F811
    """A person, not a test, places hostnames: Add a root, then Enter."""
    cfg, net, md = hosts
    b = str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None))
    r1, r2 = tmp_path / "r1", tmp_path / "r2"

    async def check(app, pilot, page):
        page.add_root(str(r1))                                     # the first root: every new hostname
        await until(pilot, lambda: page.targets == {HOST_A: str(r1), HOST_B: str(r1)})
        page.add_root(str(r2))
        page.query_one("#place-hosts").focus()
        await pilot.press("enter")                                 # cycles the first hostname's root
        await until(pilot, lambda: page.targets[HOST_A] == str(r2))
        assert not page.query_one("#btn-place-go").disabled
    _place_page(None, b, check)


def test_an_unticked_new_hostname_is_stored_and_held(hosts, tmp_path):  # noqa: F811
    cfg, net, md = hosts
    b = str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None))
    r1 = tmp_path / "r1"
    chosen = []

    async def check(app, pilot, page):
        page.add_root(str(r1))
        page.query_one("#place-hosts").focus()
        await pilot.press("space")                                 # untick the first hostname
        await until(pilot, lambda: HOST_A not in page.ticked)
        page.dismiss = chosen.append
        page.query_one("#btn-place-go").press()
        await until(pilot, lambda: chosen)
    _place_page(None, b, check)
    (c,) = chosen
    assert set(c.place) == {HOST_A, HOST_B}                        # placed either way
    r = apply_to_set(None, b, place=c.place, only=set(c.only), say=quiet)
    merged = r.merged()
    assert len(merged.updated) == 1 and len(merged.stored_held) == 1


def test_a_root_left_by_a_stopped_first_apply_still_starts(hosts, tmp_path):  # noqa: F811
    import threading

    import pytest

    from mmw.site.apply import ApplyError, nginx_steps
    cfg, net, md = hosts
    b = str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None))
    r1 = tmp_path / "r1"
    stop = threading.Event()
    with pytest.raises(ApplyError, match="stopped"):
        apply_to_set(None, b, place={"*": str(r1)}, say=quiet, stop_event=stop,
                     progress=lambda event, **kw: event == "receive" and stop.set())
    r = apply_to_set(open_set(), b, say=quiet)
    ((name, _verb, cmds),) = nginx_steps(r)
    assert cmds[-1].endswith("docker compose up -d")               # a first start, never "recreate"


def test_needs_check_each_store_once(hosts, tmp_path, monkeypatch):  # noqa: F811
    from mmw.site.apply import placement_needs
    from mmw.site.store import Store
    cfg, net, md, r1 = _applied(hosts, tmp_path)
    b = str(_bundle(cfg, net, md, tmp_path / "x", from_ref=None))
    m = open_set()
    plan = plan_set_apply(m, b)
    calls = []
    real = Store.has
    monkeypatch.setattr(Store, "has", lambda self, c: calls.append(c) or real(self, c))
    cache = {}
    for _ in range(5):                                             # five keypresses
        placement_needs(m, plan, {h: "r1" for h in plan.hosts}, cache=cache)
    distinct = {c for d in plan.host_checksums.values() for c in d}
    assert len(calls) <= len(distinct)


def test_unchanged_rows_end_unchanged(tmp_path):
    from mmw.site.apply import ApplyResult
    from mmw.tui.site.apply_flow import ApplyChoice, ApplyRunPage
    from mmw.tui.ui.base import ThemedApp

    class Quiet(ApplyRunPage):
        def _run(self):
            pass

    async def go():
        app = ThemedApp()
        async with app.run_test(size=(120, 40)) as pilot:
            page = Quiet(ApplyChoice("b.tar", frozenset(), 1, (("ka", "a"), ("kb", "b"))))
            app.push_screen(page)
            await until(pilot, lambda: page.query("#run-repos"))
            page._done(ApplyResult(updated=["ka"]))
            assert "waiting" not in str(page.query_one("#run-repos").render())
    asyncio.run(go())
