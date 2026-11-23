"""`mmw site diff` and the far side's Changes: what changed between two kept
points, read from the site's own store."""
import asyncio
import io
import lzma

import pytest
from site_apt_rows import Repo, clearsign, sha
from typer.testing import CliRunner

from mmw.cli import app
from mmw.site import home as H
from mmw.site import recents
from mmw.site.db import SiteDB
from mmw.site.roots import Roots
from mmw.site.switch import switch_in
from mmw.site.timeline import choose, ensure_state, import_rows

KEY = "https://ex.org/debian/"
DAYS = {1: {"alpha": "1.0", "beta": "2.0", "delta": "0.5"},
        2: {"alpha": "1.1", "beta": "2.0", "gamma": "1.0"},
        3: {"alpha": "1.2", "beta": "2.0", "gamma": "1.0"}}


def versioned(packages: dict, date: str) -> Repo:
    """dists/jammy with a Packages.xz naming `packages` (name → version), its debs and a Release."""
    r = Repo()
    text = "".join(f"Package: {n}\nVersion: {v}\nArchitecture: amd64\nFilename: pool/{n}_{v}.deb\n\n"
                   for n, v in packages.items()).encode()
    for n, v in packages.items():
        r.deb(f"pool/{n}_{v}.deb")
    data = lzma.compress(text)
    r.put("dists/jammy/main/binary-amd64/Packages.xz", data)
    release = (f"Origin: Ubuntu\nSuite: jammy\nCodename: jammy\nDate: {date}\nSHA256:\n"
               f" {sha(data)} {len(data)} main/binary-amd64/Packages.xz\n").encode()
    r.put("dists/jammy/Release", release)
    r.put("dists/jammy/InRelease", clearsign(release))
    return r


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "srv"
    home = H.init_root(root, name="main", set_id="s1", network="oak")
    recents.remember(str(root))
    roots = Roots(H.load_site(home))
    store = roots.store(roots.primary)
    with SiteDB(home) as db:
        for day, packages in DAYS.items():
            r = versioned(packages, f"0{day} Nov 2026 00:00:00 UTC")
            for cs, data in r.blobs.items():
                store.add_stream(io.BytesIO(data), expect=cs)
            import_rows(db, store, KEY, "ubuntu", [r.base], r.rows, {c: len(b) for c, b in r.blobs.items()},
                        f"2026110{day}T120000Z", None, set())
        vid, _w = ensure_state(db, store, KEY, choose(db, store, KEY)[0])
        switch_in(db, roots, KEY, vid, held=False)
        db.set_meta("timeline", "1")
    return root, home


def test_site_diff_to_the_next_point(site):
    root, home = site
    from mmw.site import ops
    [d] = ops.diff(home, "ubuntu", "2026-11-01")
    assert (d.suite, d.old[:8], d.new[:8]) == ("jammy", "20261101", "20261102")
    assert d.changes.upgraded == [("alpha", "amd64", "1.0", "1.1")]
    assert d.changes.added == [("gamma", "amd64", None, "1.0")]
    assert d.changes.removed == [("delta", "amd64", "0.5", None)]


def test_cli_site_diff(site):
    root, home = site
    result = CliRunner().invoke(app, ["site", "diff", "ubuntu", "2026-11-01", "2026-11-03",
                                      "--track", "jammy", "--root", str(root)])
    assert result.exit_code == 0, result.output
    assert "ubuntu / jammy: 2026-11-01 00:00:00 UTC → 2026-11-03 00:00:00 UTC · +1 ~1 −1" in result.output
    assert "~ alpha 1.0 → 1.2 (amd64)" in result.output
    newest = CliRunner().invoke(app, ["site", "diff", "ubuntu", "2026-11-03", "--root", str(root)])
    assert newest.exit_code == 1 and "is the newest point of jammy" in newest.output


def test_remote_diff_verb_round_trips(site):
    """Over ssh the far side answers `_root diff`: the same changes come back."""
    import json
    from mmw.core.point_diff import from_json
    from mmw.site.remote import serve_root_command
    root, home = site
    out = io.BytesIO()
    assert serve_root_command(["diff", str(root), "ubuntu", "2026-11-01", "", ""], io.BytesIO(), out) == 0
    [d] = from_json(json.loads(out.getvalue().decode().splitlines()[-1]))
    assert d.changes.upgraded == [("alpha", "amd64", "1.0", "1.1")]


def test_site_tui_changes_of_a_point(site):
    from mmw.site.mset import open_set
    from mmw.tui.site.app import SiteApp
    from textual.widgets import DataTable
    from waiting import shows, until

    async def go():
        app = SiteApp(open_set())
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage")
            t = app.screen.query_one("#site-history", DataTable)
            await until(pilot, lambda: t.row_count == 4)
            t.focus()
            t.move_cursor(row=1)                         # 03 Nov, served: what it changed from 02 Nov
            await pilot.pause()
            await pilot.click("#btn-site-changes")
            await until(pilot, lambda: type(app.screen).__name__ == "ChangesPage")
            await shows(pilot, lambda: app.screen.query_one("#changes-text").render(), "~ alpha 1.1 → 1.2 (amd64)")
            assert "+0 ~1 −0" in str(app.screen.query_one("#changes-text").render())
    asyncio.run(go())
