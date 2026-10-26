"""Findings from the phase 3 review, each pinned before its fix."""
import asyncio
import threading

from textual.widgets import DataTable, SelectionList

from mmw.site.apply import ApplyError, apply_bundle
from mmw.site.db import SiteDB

from test_site_apply import _bundle, _tree, env  # noqa: F401
from test_site_tui import _choose, _delta_site, _rows, _run
from test_snapshot_repo_versions import _change_a, _key
from test_bundle_mmw import _change_b
from test_dry_run_totals import two_mirrors  # noqa: F401
from waiting import until

quiet = lambda *a: None  # noqa: E731


def test_ticking_a_held_repo_serves_it(env, tmp_path):  # noqa: F811
    cfg, home, root, bundle = _delta_site(env, tmp_path, hold_a=True)

    async def check(app, pilot):
        await _choose(app, pilot, bundle)
        app.screen.query_one("#review-repos", SelectionList).select(_key(cfg, "a"))
        app.screen.query_one("#btn-review-go").press()
        await until(pilot, lambda: type(app.screen).__name__ == "ApplyRunPage" and app.screen.finished)
    _run(home, check)
    with SiteDB(home) as db:
        st = db.repo_state(_key(cfg, "a"))
        assert st.serving.number == 2 and not st.held


def test_held_but_unserved_repo_has_a_hold_note(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), only={_key(cfg, "b")}, say=quiet)

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        app.screen.query_one("#site-repos", DataTable).move_cursor(
            row=[r[0] for r in _rows(app, "site-repos")].index("a"))
        await until(pilot, lambda: "held · nothing served" in str(app.screen.query_one("#site-detail").render()))
    _run(home, check)


def _stop_on(event_name):
    stop = threading.Event()

    def progress(event, **data):
        if event == event_name:
            stop.set()
    return stop, progress


def test_stop_after_the_checks_changes_nothing_served(env, tmp_path):  # noqa: F811
    cfg, net, md, home, root = env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet)
    _change_a(md)
    nginx_before = (root / "nginx/mirror.conf").read_bytes()
    with SiteDB(home) as db:
        net_before = db.network_files()
    stop, progress = _stop_on("check")
    try:
        apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "2")), say=quiet, progress=progress,
                     stop_event=stop)
    except ApplyError as e:
        assert e.code == 5 and "stopped" in str(e)
    else:
        raise AssertionError("expected a stop")
    with SiteDB(home) as db:
        assert [v.number for v in db.versions(_key(cfg, "a"))] == [1]
        assert db.network_files() == net_before
    assert (root / "nginx/mirror.conf").read_bytes() == nginx_before


def test_stop_never_half_switches_a_hostname(env, tmp_path):  # noqa: F811
    """Both repositories live under one hostname: a stop as the first starts
    switching lets the hostname finish whole (stop is looked at between hostnames)."""
    cfg, net, md, home, root = env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet)
    _change_a(md)
    _change_b(md)
    stop, progress = _stop_on("switch")             # stop as the first repository starts switching
    r = apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "2")), say=quiet, progress=progress,
                     stop_event=stop)
    assert len(r.updated) == 2 and r.stopped == []
    with SiteDB(home) as db:
        assert db.applied()[0][0] == r.created                     # the apply is recorded


def test_receive_progress_is_throttled(env, tmp_path):  # noqa: F811
    from mmw.tui.site.apply_flow import ApplyChoice, ApplyRunPage

    class Quiet(ApplyRunPage):
        def _run(self):
            pass

    async def go():
        from mmw.tui.ui.base import ThemedApp

        class Host(ThemedApp):
            def on_mount(self):
                self.push_screen(Quiet(ApplyChoice("/x.tar", frozenset(), 10_000, ())))
        app = Host()
        async with app.run_test() as pilot:
            await until(pilot, lambda: type(app.screen).__name__ == "Quiet")
            page = app.screen
            calls = []
            real = app.call_from_thread
            app.call_from_thread = lambda fn, *a, **kw: calls.append(fn)

            def worker():
                for _ in range(2000):
                    page._progress("receive", name="x", bytes=5)
            t = threading.Thread(target=worker)
            t.start()
            t.join()
            app.call_from_thread = real
            assert len(calls) < 50 and page._pending_bytes + sum(0 for _ in calls) >= 0
    asyncio.run(go())
