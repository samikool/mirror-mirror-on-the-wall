"""Settings and upkeep from the far-side TUI, as on the command line: what
the mirror keeps (s), a suite's own keep (e), verify and repair (v),
adopted trees (o), checking signatures (g)."""
import asyncio

import pytest
from textual.widgets import DataTable

from mmw.site import recents
from mmw.site.db import SiteDB
from mmw.site.home import load_site

from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply_timeline import _apply, _bundle, env, set_a  # noqa: F401
from test_snapshot_repo_versions import _key
from site_run import run_done
from waiting import until


@pytest.fixture
def mirror(env, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg, net, md, home = env
    for d in range(1, 4):
        set_a(md, d)
        _apply(home, _bundle(cfg, net, md, tmp_path / f"b{d}"))
    recents.remember(str(home.parent.parent))
    return cfg, home, home.parent.parent


def _run(check):
    async def go():
        from mmw.site.mset import open_set
        from mmw.tui.site.app import SiteApp
        app = SiteApp(open_set())
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage")
            await check(app, pilot)
    asyncio.run(go())


def _status(app) -> str:
    return str(app.screen.query_one("#page-status").render())


def test_settings_change_what_is_kept_and_say_the_costs(mirror):
    cfg, home, root = mirror

    async def check(app, pilot):
        await pilot.press("s")
        await until(pilot, lambda: type(app.screen).__name__ == "SiteSettingsPage")
        app.screen.query_one("#set-keep").value = "5"
        app.screen.query_one("#btn-set-costs").press()
        await until(pilot, lambda: "Clean up would free" in str(app.screen.query_one("#set-costs").render()))
        app.screen.query_one("#btn-set-save").press()
        await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage" and "the newest 5" in _status(app))
    _run(check)
    assert load_site(home).keep == 5


def test_a_suites_own_keep(mirror):
    cfg, home, root = mirror
    a = _key(cfg, "a")

    async def check(app, pilot):
        page = app.screen
        t = page.query_one("#site-history", DataTable)
        await until(pilot, lambda: t.row_count >= 2)
        t.move_cursor(row=next(i for i, (tr, p, *_x) in enumerate(page._history) if p is not None and tr.name))
        await pilot.press("e")
        await until(pilot, lambda: type(app.screen).__name__ == "SuiteSettingsPage")
        app.screen.query_one("#suite-keep").value = "all"
        app.screen.query_one("#btn-suite-save").press()
        await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage" and "every point" in _status(app))
    _run(check)
    with SiteDB(home) as db:
        assert any(t.keep == "all" for t in db.tracks(a))


def test_verify_then_repair(mirror):
    cfg, home, root = mirror
    served = next(p for p in (root / "example.com").rglob("*.deb"))
    served.unlink()

    async def check(app, pilot):
        await pilot.press("v")
        await until(pilot, lambda: app.screen.query("#btn-verify-served"))
        await pilot.click("#btn-verify-served")
        await until(pilot, lambda: type(app.screen).__name__ == "ActionRunPage"
                    and "problem(s)" in run_done(app))
        app.screen.query_one("#btn-run-close").press()
        await until(pilot, lambda: type(app.screen).__name__ == "ConfirmDialog")
        app.screen.query_one("#confirm").press()
        await until(pilot, lambda: type(app.screen).__name__ == "ActionRunPage"
                    and "repaired" in run_done(app))
    _run(check)
    assert served.exists()


def test_adopted_trees_and_checking_signatures(mirror):
    async def check(app, pilot):
        await pilot.press("o")
        await until(pilot, lambda: type(app.screen).__name__ == "AdoptedPage")
        assert "Nothing adopted" in str(app.screen.query_one("#adopted-note").render())
        app.screen.query_one("#btn-adopted-close").press()
        await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage")
        await pilot.press("g")
        await until(pilot, lambda: type(app.screen).__name__ == "CheckSignaturesPage")
        app.screen.query_one("#btn-sig-go").press()
        await until(pilot, lambda: type(app.screen).__name__ == "ActionRunPage"
                    and run_done(app).strip())
        said = run_done(app)
        assert "nothing to check" in said or "no key" in said
    _run(check)


def test_settings_never_overwrite_what_they_couldnt_read(mirror, monkeypatch):
    """Only roots whose settings aren't known here (over ssh): the page asks
    only for Keep, and a root's own Clean up setting stays as it was."""
    from mmw.site.home import save_settings
    from mmw.site.mset import LocalRoot
    cfg, home, root = mirror
    save_settings(home, load_site(home).model_copy(update={"auto_clean": True}))
    monkeypatch.delattr(LocalRoot, "site_settings")

    async def check(app, pilot):
        await pilot.press("s")
        await until(pilot, lambda: type(app.screen).__name__ == "SiteSettingsPage")
        assert not app.screen.query("#set-auto")
        app.screen.query_one("#set-keep").value = "7"
        app.screen.query_one("#btn-set-save").press()
        await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage" and "the newest 7" in _status(app))
    _run(check)
    s = load_site(home)
    assert s.keep == 7 and s.auto_clean
