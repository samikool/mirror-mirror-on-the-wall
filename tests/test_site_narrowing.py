"""Say which slices a switch leaves behind and how to serve them again."""
import asyncio

import pytest
from site_apt_rows import Repo, sha
from site_format3 import write_format3
from typer.testing import CliRunner
from waiting import until

from mmw import cli
from mmw.site import home as H
from mmw.site import recents
from mmw.site.apply import apply_to_set
from mmw.site.mset import open_set
from mmw.site.tracks import split_tracks
from mmw.tui.site.app import SiteApp
from mmw.tui.site.home import SiteHomePage

KEY = "https://ex.org/debian"
WARNING = (
    "stable now serves amd64 — 2026-11-03 also had arm64: "
    "mmw site combine 'Debian' 2026-11-05 2026-11-03 --track stable --key KEY serves both "
    "(re-signed with this site's key)"
)


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "srv"
    H.init_root(root, name="main", set_id="s1", network="oak")
    recents.remember(str(root))
    return root, open_set([str(root)])


def _apply(mset, tmp_path, network, day, *arches):
    repo = Repo()
    date = f"202611{day:02d}T000000Z"
    release = f"Suite: stable\nCodename: bookworm\nDate: {day} Nov 2026 00:00:00 UTC\nSHA256:\n"
    for arch in ("amd64", "arm64"):
        package = repo.deb(f"pool/demo_{arch}.deb")
        index = f"Package: demo\nVersion: 1\nArchitecture: {arch}\nFilename: {package}\n\n".encode()
        rel = f"main/binary-{arch}/Packages"
        repo.put(f"dists/bookworm/{rel}", index)
        release += f" {sha(index)} {len(index)} {rel}\n"
    release_path = repo.put("dists/bookworm/Release", release.encode())
    files = {p: c for p, c in repo.rows.items() if p == release_path or any(a in p for a in arches)}
    (draft,) = split_tracks(files, [repo.base], repo.read, date)
    bundle = write_format3(tmp_path / f"{network}-{day}.tar", network,
                           {KEY: ("Debian", [repo.base], [(draft, date, None)])}, repo.blobs)
    result = apply_to_set(mset, bundle, place={"*": mset.roots[0]}, say=lambda _line: None)
    assert result.merged().updated == [KEY]
    return result.lines


@pytest.fixture
def narrowed_site(site, tmp_path):
    root, mset = site
    _apply(mset, tmp_path, "oak", 3, "amd64")
    _apply(mset, tmp_path, "elm", 3, "arm64")
    assert (root / "ex.org/debian/dists/bookworm/main/binary-arm64/Packages").exists()
    lines = _apply(mset, tmp_path, "oak", 5, "amd64")
    assert not (root / "ex.org/debian/dists/bookworm/main/binary-arm64/Packages").exists()
    return root, mset, lines


def test_narrower_point_is_said_on_apply(narrowed_site):
    _root, _mset, lines = narrowed_site
    assert f"Debian  {WARNING}" in [line.strip() for line in lines]


@pytest.mark.parametrize("interface", ["cli", "tui"])
def test_status_says_it_too(narrowed_site, interface):
    root, mset, _lines = narrowed_site
    if interface == "cli":
        result = CliRunner().invoke(cli.app, ["site", "status", "--root", str(root)])
        assert result.exit_code == 0, result.output
        assert WARNING in result.output
        return

    async def go():
        app = SiteApp(mset)
        async with app.run_test(size=(140, 42)) as pilot:
            def loaded():
                return (isinstance(app.screen, SiteHomePage)
                        and app.screen.query("#site-detail")
                        and "05 Nov" in str(app.screen.query_one("#site-detail").render()))
            await until(pilot, loaded)
            assert WARNING in str(app.screen.query_one("#site-detail").render())

    asyncio.run(go())


@pytest.mark.parametrize("arches", [("amd64",), ("amd64", "arm64")])
def test_wider_or_equal_says_nothing(site, tmp_path, arches):
    root, mset = site
    _apply(mset, tmp_path, "oak", 3, "amd64")
    lines = _apply(mset, tmp_path, "oak", 5, *arches)
    assert all("now serves" not in line for line in lines)
    result = CliRunner().invoke(cli.app, ["site", "status", "--root", str(root)])
    assert result.exit_code == 0, result.output
    assert "now serves" not in result.output
