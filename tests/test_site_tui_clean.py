"""Clean up from the far-side TUI: C lists what would go, Clean up does it."""
import asyncio

from mmw.site import recents
from mmw.site.db import SiteDB

from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_apply_timeline import _apply, _bundle, env, set_a  # noqa: F401
from test_snapshot_repo_versions import _key
from waiting import until


def test_clean_up_from_the_tui(env, tmp_path, monkeypatch):  # noqa: F811
    from mmw.site.mset import open_set
    from mmw.tui.site.app import SiteApp
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cfg, net, md, home = env
    for d in range(1, 6):
        set_a(md, d)
        _apply(home, _bundle(cfg, net, md, tmp_path / f"b{d}"))
    recents.remember(str(home.parent.parent))

    async def go():
        app = SiteApp(open_set())
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage")
            await pilot.press("C")
            await until(pilot, lambda: type(app.screen).__name__ == "ConfirmDialog")
            assert "2026-11-01" in str(app.screen.query_one("#confirm-message").render())
            app.screen.query_one("#confirm").press()
            await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage"
                        and "cleaned up" in str(app.screen.query_one("#page-status").render()))
    asyncio.run(go())
    with SiteDB(home) as db:
        (t,) = [t for t in db.tracks(_key(cfg, "a")) if t.name]
        assert len(db.points(t.id)) == 3
