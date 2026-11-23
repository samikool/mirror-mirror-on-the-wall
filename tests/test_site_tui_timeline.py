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
            assert rows[1][0] == "▶ 02 Nov" and rows[1][3] == "serving"
            card = str(app.screen.query_one("#site-repos", DataTable).get_row_at(0)[0])
            assert "current" in card and "2 points" not in card and "4 points" in card
    asyncio.run(go())


def test_as_of_a_bundle_counts_repositories_not_suites(tmp_path, monkeypatch):
    """One repository with two suites came in the 01 Nov bundle: "1 repository"."""
    import re
    from textual.widgets import Select
    _two_suites(tmp_path, monkeypatch)

    async def go():
        from mmw.site.mset import open_set
        from mmw.tui.site.app import SiteApp
        app = SiteApp(open_set())
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage"
                        and app.screen.query_one("#site-history", DataTable).row_count == 6)
            assert not re.search(r"\d{8}T\d{6}Z", app.export_screenshot())
            await pilot.press("b")
            await until(pilot, lambda: type(app.screen).__name__ == "ServeBundlePage"
                        and app.screen.query("#bundle-pick"))
            assert app.screen.subtitle == "every repository as that bundle left it"
            labels = [str(p) for p, _v in app.screen.query_one("#bundle-pick", Select)._options]
            assert len(labels) == 2 and all("1 repository changed" in label for label in labels), labels
            assert not any(re.search(r"\d{8}T\d{6}Z", label) for label in labels)
    asyncio.run(go())


# ── Task 12: what each point changed, what it covers, its dated URL ─────────

def _history_rows(app):
    t = app.screen.query_one("#site-history", DataTable)
    return [tuple(str(c) for c in t.get_row_at(i)) for i in range(t.row_count)]


def _site_app(check):
    async def go():
        from mmw.site.mset import open_set
        from mmw.tui.site.app import SiteApp
        app = SiteApp(open_set())
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage")
            await check(app, pilot)
    asyncio.run(go())


def _narrowed(tmp_path, monkeypatch):
    """jammy on 01 and 02 Nov for amd64 arm64, on 03 Nov for amd64 only."""
    import lzma

    from site_apt_rows import clearsign, sha
    from test_site_diff import DAYS, versioned
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "srv"
    home = H.init_root(root, name="main", set_id="s1", network="oak")
    recents.remember(str(root))
    roots = Roots(H.load_site(home))
    store = roots.store(roots.primary)
    with SiteDB(home) as db:
        for day, packages in DAYS.items():
            date = f"0{day} Nov 2026 00:00:00 UTC"
            r = versioned(packages, date)
            if day < 3:
                data = lzma.compress(b"Package: arm-only\nVersion: 1\nArchitecture: arm64\n\n")
                r.put("dists/jammy/main/binary-arm64/Packages.xz", data)
                amd = r.read(f"{r.base}/dists/jammy/main/binary-amd64/Packages.xz")
                release = (f"Origin: Ubuntu\nSuite: jammy\nCodename: jammy\nDate: {date}\nSHA256:\n"
                           + "".join(f" {sha(b)} {len(b)} main/binary-{a}/Packages.xz\n"
                                     for a, b in (("amd64", amd), ("arm64", data)))).encode()
                r.put("dists/jammy/Release", release)
                r.put("dists/jammy/InRelease", clearsign(release))
            for cs, blob in r.blobs.items():
                store.add_stream(io.BytesIO(blob), expect=cs)
            import_rows(db, store, "https://ex.org/ubuntu/", "ubuntu", [r.base], r.rows,
                        {c: len(b) for c, b in r.blobs.items()}, f"2026110{day}T120000Z", None, set())
        key = "https://ex.org/ubuntu/"
        vid, _w = ensure_state(db, store, key, choose(db, store, key)[0])
        switch_in(db, roots, key, vid, held=False)
        db.set_meta("timeline", "1")
    return home


from test_site_diff import site  # noqa: E402,F401


def test_history_leads_with_what_each_point_changed(site):  # noqa: F811
    """date, changes since the point before, size, mark — the oldest has nothing to compare with."""
    async def check(app, pilot):
        await until(pilot, lambda: len(_history_rows(app)) == 4 and _history_rows(app)[1][1])
        rows = _history_rows(app)
        assert rows[0][0] == "jammy"
        assert rows[1][:2] == ("▶ 03 Nov", "+0 ~1 −0") and rows[1][3] == "serving"
        assert rows[2][:2] == ("  02 Nov", "+1 ~1 −1")
        assert rows[3][:2] == ("  01 Nov", "")
        assert len(rows[1]) == 4                             # every point covers the same: no Covers column
        assert "point" in str(app.screen.query_one("#site-point").render())
    _site_app(check)


def test_covers_column_only_when_a_suite_narrows(tmp_path, monkeypatch):
    _narrowed(tmp_path, monkeypatch)

    async def check(app, pilot):
        await until(pilot, lambda: len(_history_rows(app)) == 4 and len(_history_rows(app)[1]) == 5)
        rows = _history_rows(app)
        assert rows[1][4] == "amd64 · main"
        assert rows[2][4] == rows[3][4] == "amd64 arm64 · main"
    _site_app(check)


def test_point_detail_says_its_sources_line(site):  # noqa: F811
    """With its day's dated tree built, the selected point says how to pin a client to it."""
    from mmw.site.dated import refresh
    root, home = site
    with SiteDB(home) as db:
        refresh(db, Roots(H.load_site(home)))

    async def check(app, pilot):
        detail = app.screen.query_one("#site-point")
        await until(pilot, lambda: "deb " in str(detail.render()))
        assert "deb http://ex.org/debian/@2026-11-03 jammy main" in str(detail.render())
        t = app.screen.query_one("#site-history", DataTable)
        t.move_cursor(row=3)
        await until(pilot, lambda: "@2026-11-01 jammy main" in str(detail.render()))
        assert "2026-11-01 00:00:00 UTC" in str(detail.render())
    _site_app(check)


def test_cli_site_history_says_changes_and_sources_line(site):  # noqa: F811
    from typer.testing import CliRunner

    from mmw.cli import app
    from mmw.site.dated import refresh
    root, home = site
    with SiteDB(home) as db:
        refresh(db, Roots(H.load_site(home)))
    result = CliRunner().invoke(app, ["site", "history", "ubuntu", "--root", str(root)])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    newest = next(line for line in lines if "2026-11-03" in line and line.startswith("▶"))
    assert "+0 ~1 −0" in newest
    assert "    deb http://ex.org/debian/@2026-11-03 jammy main" in lines


def test_remote_point_rows_verb_round_trips(site):  # noqa: F811
    """Over ssh the far side answers `_root point-rows`; asked again, the changes are read back, not reworked."""
    import json

    from mmw.site import ops
    from mmw.site.remote import serve_root_command
    root, home = site
    out = io.BytesIO()
    assert serve_root_command(["point-rows", str(root), "ubuntu"], io.BytesIO(), out) == 0
    got = json.loads(out.getvalue().decode().splitlines()[-1])
    assert sorted(r.get("changes") is not None for r in got.values()) == [False, True, True]
    assert {tuple(r["components"]) for r in got.values()} == {("main",)}
    import mmw.core.point_diff as P
    P.clear_cache()
    calls = []
    real = P.packages_of
    P.packages_of = lambda *a: calls.append(a) or real(*a)
    try:
        again = ops.point_rows(home, "ubuntu")
    finally:
        P.packages_of = real
    assert calls == [] and {str(k): v for k, v in again.items()} == got
