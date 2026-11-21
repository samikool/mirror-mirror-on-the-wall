"""`mmw site merge OLDER NEWER`: one archive held here under two URLs becomes
one repository — the older one's points join the newer one's history (an
identical Release kept once), the newer one serves, and the older one's
paths stop being served."""
import asyncio

import pytest
from typer.testing import CliRunner

from mmw import cli
from mmw.site.db import SiteDB
from test_repository_identity import _apply, bundle, release_tree, site  # noqa: F401

OLD, OLD_BASE = "http://old.deb.debian.org/debian", "old.deb.debian.org/debian"
NEW, NEW_BASE = "https://deb.debian.org/debian", "deb.debian.org/debian"


@pytest.fixture
def two(site, tmp_path):  # noqa: F811
    home, root = site
    for n, (key, label, base, day) in enumerate([(OLD, "old", OLD_BASE, 1), (NEW, "new", NEW_BASE, 5),
                                                 (OLD, "old", OLD_BASE, 3), (NEW, "new", NEW_BASE, 3)]):
        _apply(root, bundle(tmp_path / f"b{n}.tar", "oak", {key: (label, base, [release_tree(day, base=base)])},
                            created=f"2026111{n}T000000Z"))
    with SiteDB(home) as db:
        assert {st.repo for st in db.repo_states()} == {OLD, NEW}
    return home, root


def _days(home, repo):
    with SiteDB(home) as db:
        st = db.repo_state(repo)
        if st is None:
            return None
        return sorted(p.date[:8] for t in db.tracks(repo) if t.name for p in db.points(t.id, stubs=True))


def run(*args):
    return CliRunner().invoke(cli.app, ["site", *args])


def test_site_merge_moves_points_and_serves(two):
    from mmw.site.merge import merge_site_repositories
    home, root = two
    result = merge_site_repositories(home, "old", "new")
    assert (result.moved, result.same) == (1, 1)
    assert _days(home, OLD) is None
    assert _days(home, NEW) == ["20261101", "20261103", "20261105"]
    assert (root / NEW_BASE / "pool/main/h/hello/hello_5_amd64.deb").exists()
    assert not (root / OLD_BASE / "dists/bookworm/Release").exists()


def test_site_merge_dry_run(two):
    home, root = two
    r = run("merge", "old", "new", "--root", str(root), "--dry-run")
    assert r.exit_code == 0, r.output
    assert "2 point(s)" in r.output and "identical Release" in r.output
    assert _days(home, OLD) == ["20261101", "20261103"]
    assert (root / OLD_BASE / "dists/bookworm/Release").exists()


def test_site_merge_twice(two):
    home, root = two
    r = run("merge", "old", "new", "--root", str(root), "--yes")
    assert r.exit_code == 0, r.output
    r = run("merge", OLD, NEW, "--root", str(root), "--yes")
    assert r.exit_code == 0, r.output
    assert "already one repository" in r.output


def test_site_merge_refuses_unrelated(site, tmp_path):  # noqa: F811
    home, root = site
    _apply(root, bundle(tmp_path / "a.tar", "oak", {OLD: ("old", OLD_BASE, [release_tree(1, base=OLD_BASE)])}))
    _apply(root, bundle(tmp_path / "b.tar", "oak", {NEW: ("new", NEW_BASE, [release_tree(5, base=NEW_BASE,
                                                                                         suite="trixie")])},
                        created="20261116T000000Z"))
    r = run("merge", "old", "new", "--root", str(root), "--yes")
    assert r.exit_code == 1 and "nothing shows they are one repository" in r.output
    assert _days(home, OLD) == ["20261101"]


def test_site_tui_merge_action(two):
    from mmw.site.mset import open_set
    from mmw.tui.pick import PickPage
    from mmw.tui.site.app import SiteApp
    from mmw.tui.site.home import SiteHomePage
    from mmw.tui.ui.dialog import ConfirmDialog
    from waiting import settle, until
    home, root = two

    async def go():
        app = SiteApp(open_set([str(root)]))
        async with app.run_test(size=(140, 42)) as pilot:
            await until(pilot, lambda: isinstance(app.screen, SiteHomePage) and app.screen._states)
            page = app.screen
            while page.selected_repo() != OLD:
                await pilot.press("down")
            await pilot.press("M")
            await until(pilot, lambda: isinstance(app.screen, PickPage))
            app.screen.query_one("#pick-list").select(f"new ({NEW})")
            await pilot.click("#btn-done")
            await until(pilot, lambda: isinstance(app.screen, ConfirmDialog) and app.screen.query("#confirm"))
            await pilot.click("#confirm")
            await until(pilot, lambda: _days(home, OLD) is None)
            await settle(pilot)
    asyncio.run(go())
    assert _days(home, NEW) == ["20261101", "20261103", "20261105"]
