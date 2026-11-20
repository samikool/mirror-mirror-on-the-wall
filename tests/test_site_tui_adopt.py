"""Adopting from the far-side TUI: A, a folder, where it is served, Look,
Adopt — the same steps as `mmw site adopt`."""
import asyncio

from textual.widgets import Input

from mmw.site import home as H
from mmw.site import recents

from apt_tree import make_tree
from waiting import until


def test_adopt_from_the_tui(tmp_path, monkeypatch):
    from mmw.site.mset import open_set
    from mmw.tui.site.app import SiteApp
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "srv"
    H.init_root(root, name="main", set_id="s1", network="oak")
    recents.remember(str(root))
    src = make_tree(tmp_path / "old")

    async def go():
        app = SiteApp(open_set())
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage")
            await pilot.press("A")
            await until(pilot, lambda: type(app.screen).__name__ == "AdoptPage" and app.screen.query("#adopt-path"))
            app.screen.query_one("#adopt-path", Input).value = str(src)
            app.screen.query_one("#adopt-as", Input).value = "deb.debian.org/debian"
            from textual.widgets import Checkbox
            app.screen.query_one("#adopt-unverified", Checkbox).value = True
            app.screen.query_one("#btn-adopt-look").press()
            await until(pilot, lambda: "Found 1 tree" in str(app.screen.query_one("#adopt-report").render()))
            app.screen.query_one("#btn-adopt-go").press()
            await until(pilot, lambda: type(app.screen).__name__ == "AdoptRunPage" and app.screen.finished)
            assert "adopted" in str(app.screen.query_one("#adopt-out").render())
    asyncio.run(go())
    assert (root.resolve() / "deb.debian.org/debian/dists/bookworm/Release").exists()


# The old apply.py format predates `mmw site` (released through v1.2).
from test_site_adopt_apply import far  # noqa: E402, F401
from test_bundle_snapshots import states  # noqa: E402, F401


def test_first_launch_converts_apply_py_in_place_without_a_transfer(far, tmp_path, monkeypatch):  # noqa: F811
    from test_bundle_snapshots import _tree
    from mmw.commands import site as command
    from mmw.site.db import SiteDB
    from mmw.tui.site.app import SiteApp
    from mmw.tui.site.adopt_flow import AdoptRunPage
    *_rest, dest = far
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'fresh-state'))
    before = {p: ((dest / p).stat().st_ino, (dest / p).read_bytes()) for p in _tree(dest)}
    calls = []
    real = command.adopt
    def spy(path, root=None, *args, **kwargs):
        calls.append((path, root, args[4] if len(args) > 4 else kwargs.get('dry_run')))
        return real(path, root, *args, **kwargs)
    monkeypatch.setattr(command, 'adopt', spy)
    async def run():
        app = SiteApp(None)
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: app.screen.query('#start-upgrade')
                        and app.screen.query_one('#start-upgrade').is_mounted)
            app.screen.query_one('#start-upgrade').press()
            await until(pilot, lambda: type(app.screen).__name__ == 'AdoptPage' and app.screen.query('#adopt-path'))
            app.screen.query_one('#adopt-path', Input).value = str(dest)
            app.screen.query_one('#btn-adopt-look').press()
            await until(pilot, lambda: '→ in place' in str(app.screen.query_one('#adopt-report').render()))
            assert not (dest / '.mmw/site/site.db').exists()  # Look is read-only
            app.screen.query_one('#btn-adopt-go').press()
            await until(pilot, lambda: isinstance(app.screen, AdoptRunPage) and app.screen.finished)
            assert app.screen.succeeded, str(app.screen.query_one('#adopt-out').render())
            app.screen.query_one('#btn-adopt-close').press()
            await until(pilot, lambda: type(app.screen).__name__ == 'SiteHomePage'
                        and app.screen.query('#site-repos') and app.screen.query_one('#site-repos').row_count)
    asyncio.run(run())
    assert len(calls) == 2 and all(root is None for _, root, _ in calls)
    assert [dry_run for _, _, dry_run in calls] == [True, False]
    assert {p: ((dest / p).stat().st_ino, (dest / p).read_bytes()) for p in before} == before
    with SiteDB(H.root_home(dest)) as db:
        assert db.tracks('adopted:archive.ubuntu.com/ubuntu')
        assert db.get_meta('timeline') == '1'
