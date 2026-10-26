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
    """Each row's cells; a repository's card comes back as its lines."""
    t = app.screen.query_one(f"#{table_id}", DataTable)
    if table_id == "site-repos":
        return [tuple(str(t.get_row_at(i)[0]).splitlines()) for i in range(t.row_count)]
    return [tuple(str(c) for c in t.get_row_at(i)) for i in range(t.row_count)]


def _cards(app):
    t = app.screen.query_one("#site-repos", DataTable)
    return [str(t.get_row_at(i)[0]) for i in range(t.row_count)]


def _run(home, check, size=(120, 40)):
    async def go():
        from mmw.site.mset import open_set
        app = SiteApp(open_set())                   # the env root is remembered on this machine
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
        assert "v2" in a[1] and "newest v3" in a[1] and a[1].endswith("held")
        app.screen.query_one("#site-repos", DataTable).move_cursor(row=[r[0] for r in rows].index("a"))
        await until(pilot, lambda: len(_rows(app, "site-history")) == 3)
        hist = _rows(app, "site-history")
        assert hist[1][0].startswith("▶ v2") and "serving" in hist[1][3]
        assert "held at v2 — Forward serves v3" in str(app.screen.query_one("#site-detail").render())
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
                    and any(r[0] == "a" and r[1].endswith("held") for r in _rows(app, "site-repos")))
    _run(home, check)
    assert _tree(root) == trees[0]


def test_forward_and_hold_keys(env, tmp_path):  # noqa: F811
    cfg, home, root, trees = _three(env, tmp_path)
    ops.serve(home, "a", 1)

    async def check(app, pilot):
        await _pick(app, pilot, "a")
        app.screen.query_one("#site-repos", DataTable).focus()
        await pilot.press("f")
        await until(pilot, lambda: any(r[0] == "a" and r[1].endswith("current") and "v3" in r[1]
                                       for r in _rows(app, "site-repos")))
        await pilot.press("h")
        await until(pilot, lambda: any(r[0] == "a" and r[1].endswith("held") for r in _rows(app, "site-repos")))
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
        assert all(r[1].endswith("current") for r in _rows(app, "site-repos"))
    _run(home, check)


async def _choose(app, pilot, path):
    from textual.widgets import Input
    await pilot.press("a")
    await until(pilot, lambda: type(app.screen).__name__ == "PathPickPage")
    app.screen.query_one("#path-input", Input).value = str(path)
    app.screen.query_one("#btn-use").press()
    await until(pilot, lambda: type(app.screen).__name__ == "ApplyReviewPage" and app.screen.query("#btn-review-go"))


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
        assert "damaged" in done and "the mirror is unchanged" in done
    _run(home, check)
    assert _tree(root) == before


def test_apply_flow_stop_before_switch(env, tmp_path, monkeypatch):  # noqa: F811
    from test_site_apply import _tree
    from mmw.site import apply as apply_mod
    cfg, home, root, bundle = _delta_site(env, tmp_path)
    before = _tree(root)
    real = apply_mod.members_of

    def slow(*a, **kw):                 # the stop lands while the bundle is still arriving
        import time
        time.sleep(1.0)
        return real(*a, **kw)
    monkeypatch.setattr(apply_mod, "members_of", slow)

    async def check(app, pilot):
        await _review_and_go(app, pilot, bundle)
        app.screen.query_one("#btn-run-stop").press()
        await until(pilot, lambda: app.screen.finished)
        assert "stopped" in str(app.screen.query_one("#run-done").render())
    _run(home, check)
    assert _tree(root) == before


def test_tui_c_signs(env, tmp_path):  # noqa: F811
    from mmw.site import certs as C
    from mmw.site.apply import apply_bundle
    from test_site_apply import _bundle
    cfg, net, md, home, root = env
    apply_bundle(home, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=lambda *a: None)
    crt, key = C.make_ca(tmp_path / "ca", "Lab CA", None)
    C.load_ca(crt, key, None)

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        await pilot.press("c")
        await until(pilot, lambda: "signed" in str(app.screen.query_one("#page-status").render()))
    _run(home, check)
    assert (root / ".mmw/tls/mirror.crt").exists()



# ── cards, detail and buttons ────────────────────────────────────────────────

def test_cards_show_root_versions_and_size(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        card = next(c for c in _cards(app) if c.startswith("a\n"))
        assert "example.com" in card and "versions" in card and ("B" in card)
    _run(home, check)


def test_detail_says_where_what_and_how_much(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        detail = lambda: str(app.screen.query_one("#site-detail").render())  # noqa: E731
        await until(pilot, lambda: "root" in detail() and "kept" in detail())
        for word in ("hostname", "serving", "newest", "https", "last apply"):
            assert word in detail()
    _run(home, check)


def test_buttons_reach_the_actions(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        for b in ("apply", "add-root", "serve", "forward", "hold", "delete", "move", "certs", "tidy"):
            assert app.screen.query(f"#btn-site-{b}"), b
        assert app.screen.query_one("#btn-site-apply").variant == "primary"
        app.screen.query_one("#btn-site-apply").press()
        await until(pilot, lambda: type(app.screen).__name__ == "PathPickPage")
    _run(home, check)


def test_history_still_loads_when_a_root_cannot_say_its_details(env, tmp_path, monkeypatch):  # noqa: F811
    # An older mmw over ssh has no `details` verb.
    from mmw.site.mset import LocalRoot
    cfg, home, root, _ = _three(env, tmp_path)

    def older(self):
        raise ValueError("unknown verb 'details'")
    monkeypatch.setattr(LocalRoot, "details", older)

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        await until(pilot, lambda: len(_rows(app, "site-history")) >= 1)
        assert "serving" in str(app.screen.query_one("#site-detail").render())
    _run(home, check)


def test_version_buttons_are_on_screen_at_80x24(env, tmp_path):  # noqa: F811
    cfg, home, root, _ = _three(env, tmp_path)

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        await until(pilot, lambda: len(_rows(app, "site-history")) == 3)
        await until(pilot, lambda: "last apply" in str(app.screen.query_one("#site-detail").render()))

        def fits():                     # once the filled-in detail has been laid out
            split = app.screen.query_one("#site-split").region
            buttons = [app.screen.query_one(f"#btn-site-{b}").region for b in ("serve", "forward", "hold", "delete")]
            return (all(b.height == 1 and b.bottom <= split.bottom for b in buttons)
                    and app.screen.query_one("#site-history").region.height >= 4)
        await until(pilot, fits)
    _run(home, check, size=(80, 24))


def _detail_plain(states, repo, rows, details):
    from textual.content import Content
    from mmw.tui.site.home import SiteHomePage
    page = SiteHomePage()
    page._states = states
    return Content.from_markup(page._detail_text(repo, rows, details)).plain


def test_detail_shows_brackets_in_names_and_paths():
    from types import SimpleNamespace
    s = SimpleNamespace(repo="r", label="mirror [eu]", held=False, serving=None, bases=["x.org/[a]"])
    plain = _detail_plain([s], "r", [], {"root": "oak", "location": "/srv/[Mirror]\\", "https": "signed [by CA]"})
    for text in ("mirror [eu]", "/srv/[Mirror]\\", "signed [by CA]"):
        assert text in plain, plain


def test_detail_says_held_with_no_versions_kept():
    from types import SimpleNamespace
    s = SimpleNamespace(repo="r", label="r", held=True, serving=None, bases=[])
    assert "held" in _detail_plain([s], "r", [], {})


def test_a_failed_reload_keeps_where_each_repository_lives(env, tmp_path, monkeypatch):  # noqa: F811
    from mmw.site.mset import LocalRoot
    cfg, home, root, _ = _three(env, tmp_path)

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        before = dict(app.screen._root_of)

        def down(self, repo):
            raise OSError("root went away")
        monkeypatch.setattr(LocalRoot, "history", down)
        app.screen.action_refresh()
        await until(pilot, lambda: "went away" in str(app.screen.query_one("#page-status").render()))
        assert app.screen._root_of == before and before
    _run(home, check)


def test_actions_wait_while_one_is_running(env, tmp_path, monkeypatch):  # noqa: F811
    import threading
    from textual.widgets import Button
    cfg, home, root, _ = _three(env, tmp_path)
    go = threading.Event()
    calls = []

    def slow(mset, path):
        calls.append(path)
        go.wait(20)
        return "new root x"
    monkeypatch.setattr(ops, "add_root", slow)

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        page = app.screen
        page.add_root_path("lnxp005:/data")
        await until(pilot, lambda: "Adding root" in str(page.query_one("#page-status").render()))
        await until(pilot, lambda: page.query_one("#btn-site-tidy", Button).disabled)
        assert not page.check_action("tidy", ())
        page.query_one("#btn-site-add-root", Button).press()
        await pilot.pause()
        go.set()
        await until(pilot, lambda: not page.query_one("#btn-site-tidy", Button).disabled)
        assert calls == ["lnxp005:/data"]
    _run(home, check)
