"""Far-side local packages in the TUI."""
import asyncio
import shutil

import pytest
from textual.widgets import ListView, Select

from debs import make_deb
from mmw.core.layers import read_decisions
from mmw.site.db import SiteDB
from mmw.site.layers import site_layer_dir
from test_add_source import _Harness
from test_layers import lab  # noqa: F401
from test_site_layers import site  # noqa: F401
from waiting import until

pytestmark = pytest.mark.skipif(shutil.which("gpg") is None or shutil.which("gpgv") is None,
                                reason="needs gpg and gpgv")


def _page(site):  # noqa: F811
    from mmw.site.home import load_site
    from mmw.site.roots import Roots
    from mmw.tui.site.packages import SitePackagesPage
    roots = Roots(load_site(site.home))
    return lambda: SitePackagesPage(site.home, roots.store(roots.primary), site.repo)


def test_saving_resign_re_signs_what_is_served(site):  # noqa: F811
    async def scenario():
        app = _Harness(_page(site))
        async with app.run_test(size=(120, 40)) as pilot:
            page = app.screen
            await until(pilot, lambda: page.query("#spkg-signing"))
            page.query_one("#spkg-signing", Select).value = "resign"
            page.query_one("#spkg-key", Select).value = "site"
            await pilot.pause()
            page.query_one("#btn-spkg-save").press()
            await until(pilot, lambda: "Saved" in str(page.query_one("#page-status").render()))
    asyncio.run(scenario())
    with SiteDB(site.home) as db:
        assert db.layer(site.repo).key == "site"
    upstream = (site.lab.up.root / "ubuntu" / "dists/jammy/InRelease").read_bytes()
    assert (site.root / site.base / "dists/jammy/InRelease").read_bytes() != upstream


def test_adding_a_clashing_deb_asks(site):  # noqa: F811
    with SiteDB(site.home) as db:
        db.set_layer(site.repo, signing="unsigned")
    clash = make_deb(site.lab.tmp / "new", "hello", "2.9")

    async def scenario():
        app = _Harness(_page(site))
        async with app.run_test(size=(120, 40)) as pilot:
            page = app.screen
            await until(pilot, lambda: page.query("#spkg-debs"))
            page.start_flow(lambda: page.add_debs([clash], "jammy", "main"))
            await until(pilot, lambda: type(app.screen).__name__ == "ClashDialog"
                        and app.screen.query("#btn-clash-drop"))      # pushed AND built
            app.screen.query_one("#btn-clash-drop").press()
            await until(pilot, lambda: len(page.query_one("#spkg-debs", ListView).children) == 1)
    asyncio.run(scenario())
    folder = site_layer_dir(site.home, site.repo)
    assert read_decisions(folder) == {"hello_2.9_amd64.deb": "drop"}
