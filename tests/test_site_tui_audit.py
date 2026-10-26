"""The far-side TUI audit before 1.3.0: nothing slow on the screen's thread,
progress wherever there's work to watch, nothing cut off at 80 columns."""
import threading

from textual.widgets import Button, DataTable, Static

from mmw.site import ops
from test_site_tui import _rows, _run, _three, env  # noqa: F401
from test_dry_run_totals import two_mirrors  # noqa: F401
from waiting import until


def _name(app):
    return type(app.screen).__name__


def _on(app, page, wid):
    """`page` is on screen and drawn: its `wid`'s text, else ''."""
    if _name(app) != page or not app.screen.query(wid):
        return ""
    return str(app.screen.query_one(wid).render())


async def _pick_version(app, pilot, label, number):
    home = app.screen
    t = home.query_one("#site-repos", DataTable)
    t.move_cursor(row=[r[0] for r in _rows(app, "site-repos")].index(label))
    await until(pilot, lambda: any(r[0].strip().lstrip("▶ ") == f"v{number}" for r in _rows(app, "site-history")))
    h = home.query_one("#site-history", DataTable)
    h.move_cursor(row=[r[0].strip().lstrip("▶ ") for r in _rows(app, "site-history")].index(f"v{number}"))
    return home


# ── nothing slow on the screen's thread ──────────────────────────────────────

def test_serve_works_out_its_preview_off_the_screen_and_shows_the_switch(env, tmp_path, monkeypatch):  # noqa: F811
    from mmw.site.mset import LocalRoot
    cfg, home_dir, root, _ = _three(env, tmp_path)
    gate = threading.Event()
    real = LocalRoot.serve_preview

    def slow(self, key, number):
        gate.wait(20)
        return real(self, key, number)
    monkeypatch.setattr(LocalRoot, "serve_preview", slow)

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        home = await _pick_version(app, pilot, "a", 1)
        home.action_serve()                                      # returns at once: the screen stays live
        await until(pilot, lambda: "working out" in str(home.query_one("#page-status").render()))
        gate.set()
        await until(pilot, lambda: _name(app) == "ConfirmDialog")
        app.screen.query_one("#confirm").press()
        await until(pilot, lambda: "serves v1" in _on(app, "SwitchRunPage", "#run-done"))
        assert "file" in str(app.screen.query_one("#run-rate").render())     # files placed, counted
        app.screen.query_one("#btn-run-close").press()
        await until(pilot, lambda: _name(app) == "SiteHomePage")
        await until(pilot, lambda: any(r[0] == "a" and r[1].startswith("▶ v1") for r in _rows(app, "site-repos")))
    _run(home_dir, check)


def test_forward_says_when_there_is_nothing_to_do(env, tmp_path):  # noqa: F811
    cfg, home_dir, root, _ = _three(env, tmp_path)

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        home = await _pick_version(app, pilot, "a", 3)
        home.action_forward()
        await until(pilot, lambda: "already at its newest" in str(home.query_one("#page-status").render()))
        assert _name(app) == "SiteHomePage"
    _run(home_dir, check)


def test_forward_shows_the_switch(env, tmp_path):  # noqa: F811
    cfg, home_dir, root, _ = _three(env, tmp_path)
    ops.serve(home_dir, "a", 1)

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        home = await _pick_version(app, pilot, "a", 1)
        home.action_forward()
        await until(pilot, lambda: "serves v3" in _on(app, "SwitchRunPage", "#run-done"))
    _run(home_dir, check)


def test_tidy_runs_on_the_progress_page_and_says_when_there_is_nothing(env, tmp_path):  # noqa: F811
    cfg, home_dir, root, _ = _three(env, tmp_path)

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        app.screen.action_tidy()
        await until(pilot, lambda: "nothing to tidy" in _on(app, "TidyRunPage", "#run-done"))
    _run(home_dir, check)


def test_move_works_out_its_preview_off_the_screen(two_mirrors, tmp_path, monkeypatch):  # noqa: F811
    from textual.widgets import Select
    from mmw.site.mset import open_set
    from mmw.tui.site.app import SiteApp
    from test_site_set_ops import _one_hostname_two_roots
    import asyncio
    _one_hostname_two_roots(two_mirrors, tmp_path)
    gate = threading.Event()
    real = ops.move_preview
    monkeypatch.setattr(ops, "move_preview", lambda *a: (gate.wait(20), real(*a))[1])

    async def go():
        app = SiteApp(open_set())
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: _name(app) == "SiteHomePage")
            home = app.screen
            await until(pilot, lambda: home.query_one("#site-repos", DataTable).row_count == 2)
            home.action_move()
            await until(pilot, lambda: _name(app) == "MoveRootPage" and app.screen.query("#move-root"))
            app.screen.query_one("#move-root", Select).value = "r2"
            app.screen.query_one("#btn-move-go").press()
            await until(pilot, lambda: _name(app) == "SiteHomePage"
                        and "working out" in str(home.query_one("#page-status").render()))
            gate.set()
            await until(pilot, lambda: _name(app) == "ConfirmDialog")
    asyncio.run(go())


# ── 80 columns ───────────────────────────────────────────────────────────────

def test_cards_keep_their_state_at_80_columns(env, tmp_path):  # noqa: F811
    cfg, home_dir, root, _ = _three(env, tmp_path)
    ops.serve(home_dir, "a", 2)

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        a = next(r for r in _rows(app, "site-repos") if r[0] == "a")
        assert a[1].startswith("▶ v2 · ") and a[1].endswith("held · newest v3")
        key = next(st.repo for st in app.screen._states if st.label == "a")
        assert a[2].startswith(f"{app.screen._root_of[key]} · 3 versions · ")
        b = next(r for r in _rows(app, "site-repos") if r[0] == "b")
        assert b[1].endswith("current")
        width = app.screen.query_one("#site-repos").region.width
        for r in _rows(app, "site-repos"):
            assert all(len(line) + 2 <= width for line in r), (r, width)
    _run(home_dir, check, size=(80, 24))


def test_a_long_message_is_read_in_full(env, tmp_path):  # noqa: F811
    cfg, home_dir, root, _ = _three(env, tmp_path)
    long = ("debian-deb-security can't move on its own: it shares deb.debian.org with debian-deb, "
            "and a hostname is served from one root")

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        home = app.screen
        home.set_status(long, "error")
        await pilot.pause(0.2)
        status = home.query_one("#page-status", Static)
        assert status.region.height > 1
        assert "served from one root" in str(status.render())
        # …and every button is still on the screen
        for b in home.query("#page-actions Button"):
            assert b.region.height == 1 and b.region.right <= 80
    _run(home_dir, check, size=(80, 24))


def test_the_footer_fits_80_columns():
    from mmw.tui.site.home import SiteHomePage
    shown = [b for b in SiteHomePage.BINDINGS if b.show]
    width = sum(len(b.key_display or b.key) + len(b.description) + 3 for b in shown) + len("q Quit") + 3
    assert width <= 80, width


# ── names and messages ───────────────────────────────────────────────────────

def test_page_names_say_what_they_are():
    from mmw.tui.site.home import ServeBundlePage, SiteHomePage
    assert SiteHomePage.TITLE == "Repositories"
    assert ServeBundlePage.TITLE == "As of a bundle"


def test_last_apply_is_the_mirrors(env, tmp_path, monkeypatch):  # noqa: F811
    from mmw.site.mset import LocalRoot
    cfg, home_dir, root, _ = _three(env, tmp_path)
    real = LocalRoot.details
    calls = []
    monkeypatch.setattr(LocalRoot, "details", lambda self: calls.append(1) or real(self))

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        detail = lambda: str(app.screen.query_one("#site-detail").render())  # noqa: E731
        await until(pilot, lambda: "last apply" in detail())
        assert "mirror's last apply" in detail() or "last apply" in detail()
        assert app.screen._last_apply is not None
    _run(home_dir, check)


def test_add_root_opens_beside_the_mirrors_roots(env, tmp_path):  # noqa: F811
    cfg, home_dir, root, _ = _three(env, tmp_path)

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        app.screen.action_add_root()
        await until(pilot, lambda: _name(app) == "PathPickPage")
        assert app.screen._root == root.parent
    _run(home_dir, check)


def test_apply_review_says_where_an_ssh_root_is():
    from pathlib import Path
    from mmw.site.apply import ApplyPlan, PlanRow
    from mmw.tui.site.apply_flow import _row_text
    row = PlanRow("u", "debian", 1, 2, 10, 1000, 1, False, False, Path("/data"))
    assert "on nas · /data" in _row_text(row, "nas:/data")
    assert "/mnt/oak" in _row_text(row, "/mnt/oak")


# ── certificates with no CA yet ──────────────────────────────────────────────

def test_certs_with_no_ca_offers_to_load_or_make_one(env, tmp_path):  # noqa: F811
    cfg, home_dir, root, _ = _three(env, tmp_path)

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        app.screen.action_certs()
        await until(pilot, lambda: _name(app) == "CaSetupPage" and app.screen.query("#btn-ca-load"))
        assert app.screen.query("#btn-ca-make")
    _run(home_dir, check)


def test_making_a_ca_from_the_tui_then_signs(env, tmp_path):  # noqa: F811
    from mmw.site import recents
    cfg, home_dir, root, _ = _three(env, tmp_path)
    where = tmp_path / "ca"

    async def check(app, pilot):
        await until(pilot, lambda: len(_rows(app, "site-repos")) == 2)
        home = app.screen
        home.make_ca(str(where), "Lab CA", None)
        await until(pilot, lambda: "signed" in str(home.query_one("#page-status").render()))
        assert recents.ca_paths() is not None and (root / ".mmw" / "tls" / "mirror.crt").exists()
    _run(home_dir, check)
