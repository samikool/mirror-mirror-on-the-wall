"""The far-side TUI: repositories and their history, acting on versions,
and applying a bundle — all through mmw.site's own operations."""
import asyncio

from textual.widgets import DataTable

from mmw.site import ops
from mmw.tui.site.app import SiteApp

from test_site_apply import env  # noqa: F401
from test_site_ops import _three
from test_dry_run_totals import two_mirrors  # noqa: F401
from waiting import until


def _rows(app, table_id):
    t = app.screen.query_one(f"#{table_id}", DataTable)
    return [tuple(str(c) for c in t.get_row_at(i)) for i in range(t.row_count)]


def _run(home, check, size=(120, 40)):
    async def go():
        app = SiteApp(home)
        async with app.run_test(size=size) as pilot:
            await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage")
            await check(app, pilot)
    asyncio.run(go())


def test_lists_repos_and_history(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)
    ops.serve(home, "a", 2)

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        rows = _rows(app, "site-repos")
        a = next(r for r in rows if r[0] == "a")
        assert "v2" in a[1] and "v3" in a[2] and a[3] == "held"
        app.screen.query_one("#site-repos", DataTable).move_cursor(row=[r[0] for r in rows].index("a"))
        await until(pilot, lambda: len(_rows(app, "site-history")) == 3)
        hist = _rows(app, "site-history")
        assert hist[1][0].startswith("▶ v2") and "serving" in hist[1][3]
        assert "f forward to v3" in str(app.screen.query_one("#site-hold-note").render())
    _run(home, check)


def test_empty_site_says_apply(env):  # noqa: F811
    _cfg, _net, _md, home, _root = env

    async def check(app, pilot):
        await until(pilot, lambda: "nothing applied yet" in str(app.screen.query_one("#page-status").render()))
    _run(home, check)


async def _pick(app, pilot, repo_label, version=None):
    t = app.screen.query_one("#site-repos", DataTable)
    await until(pilot, lambda: t.row_count > 0)
    t.move_cursor(row=[r[0] for r in _rows(app, "site-repos")].index(repo_label))
    if version is not None:
        h = app.screen.query_one("#site-history", DataTable)
        await until(pilot, lambda: any(r[0].strip().lstrip("▶ ") == f"v{version}" for r in _rows(app, "site-history")))
        h.move_cursor(row=[r[0].strip().lstrip("▶ ") for r in _rows(app, "site-history")].index(f"v{version}"))
        h.focus()


def test_serve_from_history_confirms_then_switches(env, tmp_path):  # noqa: F811
    cfg, home, root, trees = _three(env, tmp_path)
    from test_site_apply import _tree

    async def check(app, pilot):
        await _pick(app, pilot, "a", 1)
        await pilot.press("enter")
        await until(pilot, lambda: type(app.screen).__name__ == "ConfirmDialog")
        details = str(app.screen.query_one("#confirm-details Label").render())
        assert "now serving v3" in details and "nothing to copy" in details and "indexes switch last" in details
        app.screen.query_one("#confirm").press()
        await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage"
                    and any(r[0] == "a" and r[3] == "held" for r in _rows(app, "site-repos")))
    _run(home, check)
    assert _tree(root) == trees[0]


def test_forward_and_hold_keys(env, tmp_path):  # noqa: F811
    cfg, home, root, trees = _three(env, tmp_path)
    ops.serve(home, "a", 1)

    async def check(app, pilot):
        await _pick(app, pilot, "a")
        app.screen.query_one("#site-repos", DataTable).focus()
        await pilot.press("f")
        await until(pilot, lambda: any(r[0] == "a" and r[3] == "current" and "v3" in r[1]
                                       for r in _rows(app, "site-repos")))
        await pilot.press("h")
        await until(pilot, lambda: any(r[0] == "a" and r[3] == "held" for r in _rows(app, "site-repos")))
    _run(home, check)


def test_delete_refusal_is_a_status(env, tmp_path):  # noqa: F811
    cfg, home, root, trees = _three(env, tmp_path)

    async def check(app, pilot):
        await _pick(app, pilot, "a", 3)
        await pilot.press("d")
        await until(pilot, lambda: type(app.screen).__name__ == "ConfirmDialog")
        app.screen.query_one("#confirm").press()
        await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage"
                    and "being served" in str(app.screen.query_one("#page-status").render()))
        await _pick(app, pilot, "a", 1)
        await pilot.press("d")
        await until(pilot, lambda: type(app.screen).__name__ == "ConfirmDialog")
        app.screen.query_one("#confirm").press()
        await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage"
                    and len(_rows(app, "site-history")) == 2)
    _run(home, check)


def test_lock_held_is_a_status(env, tmp_path):  # noqa: F811
    from mmw.site.lock import SiteLock
    cfg, home, root, trees = _three(env, tmp_path)

    async def check(app, pilot):
        await _pick(app, pilot, "a")
        app.screen.query_one("#site-repos", DataTable).focus()
        with SiteLock(home):
            await pilot.press("h")
            await until(pilot, lambda: "in use by" in str(app.screen.query_one("#page-status").render()))
        assert all(r[3] == "current" for r in _rows(app, "site-repos"))
    _run(home, check)


async def _choose(app, pilot, path):
    from textual.widgets import Input
    await pilot.press("a")
    await until(pilot, lambda: type(app.screen).__name__ == "PathPickPage")
    app.screen.query_one("#path-input", Input).value = str(path)
    app.screen.query_one("#btn-use").press()
    await until(pilot, lambda: type(app.screen).__name__ == "ApplyReviewPage")


def _delta_site(env, tmp_path, hold_a=False):  # noqa: F811
    from mmw.site.apply import apply_bundle
    from test_site_apply import _bundle
    from test_snapshot_repo_versions import _change_a
    from test_bundle_mmw import _change_b
    cfg, net, md, home, root = env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=lambda *a: None)
    if hold_a:
        ops.hold(home, "a")
    _change_a(md)
    _change_b(md)
    return cfg, home, root, _bundle(cfg, net, md, tmp_path / "2")


def test_review_ticks_all_but_held(env, tmp_path):  # noqa: F811
    from textual.widgets import SelectionList
    from test_snapshot_repo_versions import _key
    cfg, home, root, bundle = _delta_site(env, tmp_path, hold_a=True)

    async def check(app, pilot):
        await _choose(app, pilot, bundle)
        sl = app.screen.query_one("#review-repos", SelectionList)
        assert set(sl.selected) == {_key(cfg, "b")}
        notes = str(app.screen.query_one("#review-notes").render())
        assert "held at v1" in notes and "stored either way" in notes
        assert "always applied" in str(app.screen.query_one("#review-totals").render())
    _run(home, check)


def test_review_untick_note(env, tmp_path):  # noqa: F811
    from textual.widgets import SelectionList
    from test_snapshot_repo_versions import _key
    cfg, home, root, bundle = _delta_site(env, tmp_path)

    async def check(app, pilot):
        await _choose(app, pilot, bundle)
        sl = app.screen.query_one("#review-repos", SelectionList)
        sl.deselect(_key(cfg, "a"))
        await until(pilot, lambda: "stored and held" in str(app.screen.query_one("#review-notes").render()))
    _run(home, check)


def test_review_of_a_non_bundle_is_a_status(env, tmp_path):  # noqa: F811
    from textual.widgets import Input
    _cfg, _net, _md, home, _root = env
    junk = tmp_path / "junk.tar"
    junk.write_text("not a tar")

    async def check(app, pilot):
        await pilot.press("a")
        await until(pilot, lambda: type(app.screen).__name__ == "PathPickPage")
        app.screen.query_one("#path-input", Input).value = str(junk)
        app.screen.query_one("#btn-use").press()
        await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage"
                    and "not a bundle" in str(app.screen.query_one("#page-status").render()))
    _run(home, check)


async def _review_and_go(app, pilot, bundle):
    await _choose(app, pilot, bundle)
    app.screen.query_one("#btn-review-go").press()
    await until(pilot, lambda: type(app.screen).__name__ == "ApplyRunPage" and app.screen.query("#btn-run-stop"))


def test_apply_flow_end_to_end(env, tmp_path):  # noqa: F811
    cfg, home, root, bundle = _delta_site(env, tmp_path)

    async def check(app, pilot):
        await _review_and_go(app, pilot, bundle)
        await until(pilot, lambda: app.screen.finished)
        done = str(app.screen.query_one("#run-done").render())
        assert "a  v1 → v2" in done and "b  v1 → v2" in done
        app.screen.query_one("#btn-run-close").press()
        await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage"
                    and all("v2" in r[1] for r in _rows(app, "site-repos")))
    _run(home, check)


def test_apply_flow_shows_a_failed_check(env, tmp_path):  # noqa: F811
    from test_site_apply import _rewrite, _tree
    cfg, home, root, bundle = _delta_site(env, tmp_path)
    bad = _rewrite(bundle, tmp_path / "bad.tar", lambda m, d: d[:-1] + b"!" if m.name.endswith(".deb") else d)
    before = _tree(root)

    async def check(app, pilot):
        await _review_and_go(app, pilot, bad)
        await until(pilot, lambda: app.screen.finished)
        done = str(app.screen.query_one("#run-done").render())
        assert "damaged" in done and "the site is unchanged" in done
    _run(home, check)
    assert _tree(root) == before


def test_apply_flow_stop_before_switch(env, tmp_path, monkeypatch):  # noqa: F811
    from test_site_apply import _tree
    from mmw.site import apply as apply_mod
    cfg, home, root, bundle = _delta_site(env, tmp_path)
    before = _tree(root)
    real = apply_mod._receive

    def slow(*a, **kw):                 # the stop lands while the bundle is still arriving
        import time
        time.sleep(1.0)
        return real(*a, **kw)
    monkeypatch.setattr(apply_mod, "_receive", slow)

    async def check(app, pilot):
        await _review_and_go(app, pilot, bundle)
        app.screen.query_one("#btn-run-stop").press()
        await until(pilot, lambda: app.screen.finished)
        assert "stopped" in str(app.screen.query_one("#run-done").render())
    _run(home, check)
    assert _tree(root) == before


def test_root_column_with_two_roots(two_mirrors, tmp_path):  # noqa: F811
    import pytest as _p  # noqa: F401
    from test_site_roots import split_env as _split  # noqa: F401
    from mmw.site import home as H
    from mmw.site.apply import apply_bundle
    from test_site_apply import _bundle
    cfg, net, md, home, main, second = _split.__wrapped__(tmp_path)
    kb = next(s.identity for s in cfg.sources if s.name == "b")
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), place={kb: "second"},
                 say=lambda *a: None)

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        cols = [str(c.label) for c in app.screen.query_one("#site-repos", DataTable).columns.values()]
        assert "Root" in cols
        rows = {r[0]: r for r in _rows(app, "site-repos")}
        assert rows["b"][cols.index("Root")] == "second" and rows["a"][cols.index("Root")] == "main"
    _run(home, check)
    assert H.load_site(home).roots[1].name == "second"


def test_move_key(two_mirrors, tmp_path):  # noqa: F811
    from textual.widgets import Select
    from test_site_roots import split_env as _split
    from mmw.site.apply import apply_bundle
    from mmw.site.db import SiteDB
    from test_site_apply import _bundle
    cfg, net, md, home, main, second = _split.__wrapped__(tmp_path)
    kb = next(s.identity for s in cfg.sources if s.name == "b")
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=lambda *a: None)

    async def check(app, pilot):
        await _pick(app, pilot, "b")
        app.screen.query_one("#site-repos", DataTable).focus()
        await pilot.press("m")
        await until(pilot, lambda: type(app.screen).__name__ == "MoveRootPage")
        app.screen.query_one("#move-root", Select).value = "second"
        app.screen.query_one("#btn-move-go").press()
        await until(pilot, lambda: type(app.screen).__name__ == "ConfirmDialog")
        assert "Copies" in str(app.screen.query_one("#confirm-message").render())
        app.screen.query_one("#confirm").press()
        await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage"
                    and "moved" in str(app.screen.query_one("#page-status").render()))
    _run(home, check)
    with SiteDB(home) as db:
        assert db.repo_state(kb).root == "second"


def test_tui_c_signs(env, tmp_path):  # noqa: F811
    from mmw.site import certs as C
    from mmw.site.apply import apply_bundle
    from test_site_apply import _bundle
    cfg, net, md, home, root = env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=lambda *a: None)
    crt, key = C.make_ca(tmp_path / "ca", "Lab CA", None)
    C.load_ca(home, crt, key, None)

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        await pilot.press("c")
        await until(pilot, lambda: "signed" in str(app.screen.query_one("#page-status").render()))
    _run(home, check)
    assert (root / ".mmw/tls/mirror.crt").exists()
