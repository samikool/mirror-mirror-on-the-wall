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
