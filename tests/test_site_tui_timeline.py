"""The site TUI's history pane, by suite: a repository with two suites shows
each heading with its points under it, served ones marked."""
import asyncio
import io

from site_apt_rows import Repo
from textual.widgets import DataTable

from mmw.site import home as H
from mmw.site import recents
from mmw.site.db import SiteDB
from mmw.site.roots import Roots
from mmw.site.switch import switch_in
from mmw.site.timeline import choose, ensure_state, import_rows
from waiting import until

KEY = "https://ex.org/debian/"


def _two_suites(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "srv"
    home = H.init_root(root, name="main", set_id="s1", network="oak")
    recents.remember(str(root))
    roots = Roots(H.load_site(home))
    store = roots.store(roots.primary)
    with SiteDB(home) as db:
        for day in (1, 2):
            r = Repo()
            r.deb(f"pool/p_{day}.deb")
            r.suite("bookworm", f"0{day} Nov 2026 00:00:00 UTC", [f"pool/p_{day}.deb"])
            r.suite("bookworm-updates", f"0{day} Nov 2026 06:00:00 UTC", [f"pool/p_{day}.deb"])
            for cs, data in r.blobs.items():
                store.add_stream(io.BytesIO(data), expect=cs)
            import_rows(db, store, KEY, "debian", [r.base], r.rows, {c: len(b) for c, b in r.blobs.items()},
                        f"2026110{day}T120000Z", None, set())
        vid, _w = ensure_state(db, store, KEY, choose(db, store, KEY)[0])
        switch_in(db, roots, KEY, vid, held=False)
        db.set_meta("timeline", "1")  # this fixture constructs modern points directly
    return home


def test_two_suites_show_their_headings_and_points(tmp_path, monkeypatch):
    _two_suites(tmp_path, monkeypatch)

    async def go():
        from mmw.site.mset import open_set
        from mmw.tui.site.app import SiteApp
        app = SiteApp(open_set())
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage")
            t = app.screen.query_one("#site-history", DataTable)
            await until(pilot, lambda: t.row_count == 6)
            rows = [tuple(str(c) for c in t.get_row_at(i)) for i in range(t.row_count)]
            assert [r[0] for r in rows if not r[0].startswith(("▶", "  "))] == \
                ["bookworm", "bookworm-updates"]
            assert sum(1 for r in rows if r[0].startswith("▶")) == 2          # one served per suite
            assert "02 Nov" in rows[1][1] and rows[1][3] == "serving"
            card = str(app.screen.query_one("#site-repos", DataTable).get_row_at(0)[0])
            assert "current" in card and "2 points" not in card and "4 points" in card
    asyncio.run(go())
