"""Serving a widened APT point keeps the site's pin and hold semantics."""
import pytest
from site_apt_rows import Repo, sha
from site_format3 import write_format3
from site_points import nth

from mmw.site import home as H
from mmw.site import ops
from mmw.site.apply import apply_bundle, apply_to_set
from mmw.site.db import SiteDB
from mmw.site.mset import open_set
from mmw.site.tracks import split_tracks

KEY = "https://ex.org/debian"
DIST = "ex.org/debian/dists/bookworm"
AMD = f"{DIST}/main/binary-amd64/Packages"
ARM = f"{DIST}/main/binary-arm64/Packages"


def _quiet(*args):
    pass


@pytest.fixture
def widened(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "srv"
    home = H.init_root(root, name="main", set_id="s1", network="oak")
    repo = Repo()
    date = "20261101T000000Z"
    release = "Suite: stable\nCodename: bookworm\nDate: 01 Nov 2026 00:00:00 UTC\nSHA256:\n"
    for arch in ("amd64", "arm64"):
        package = repo.deb(f"pool/demo_{arch}.deb")
        index = f"Package: demo\nVersion: 1\nArchitecture: {arch}\nFilename: {package}\n\n".encode()
        rel = f"main/binary-{arch}/Packages"
        repo.put(f"dists/bookworm/{rel}", index)
        release += f" {sha(index)} {len(index)} {rel}\n"
    release_path = repo.put("dists/bookworm/Release", release.encode())

    def bundle(network, arch):
        files = {p: c for p, c in repo.rows.items() if p == release_path or arch in p}
        (draft,) = split_tracks(files, [repo.base], repo.read, date)
        return write_format3(tmp_path / f"{network}-{arch}.tar", network,
                             {KEY: ("Debian", [repo.base], [(draft, date, None)])}, repo.blobs)

    initial = apply_bundle(home, bundle("oak", "amd64"), say=_quiet)
    assert initial.updated == [KEY]
    assert (root / AMD).is_file()
    assert not (root / ARM).exists()
    return root, home, bundle


def test_same_network_widening_is_served(widened):
    root, home, bundle = widened
    result = apply_bundle(home, bundle("oak", "arm64"), say=_quiet)
    assert (root / ARM).is_file()
    assert (root / AMD).is_file()
    assert result.updated == [KEY]
    assert any("stable 2026-11-01 widened" in line for line in result.lines)


def test_foreign_widening_is_served_through_the_set(widened):
    root, _home, bundle = widened
    result = apply_to_set(open_set([str(root)]), bundle("elm", "arm64"), say=_quiet)
    assert (root / ARM).is_file()
    assert (root / AMD).is_file()
    assert result.merged().updated == [KEY]
    assert any("stable 2026-11-01 widened" in line for line in result.lines)


def test_widening_a_pinned_point_refreshes_it(widened):
    root, home, bundle = widened
    ops.serve(home, KEY, nth(home, KEY, 1))
    with SiteDB(home) as db:
        (before,) = db.tracks(KEY)
        assert before.pinned is not None

    result = apply_bundle(home, bundle("oak", "arm64"), say=_quiet)
    assert (root / ARM).is_file()
    assert result.updated == [KEY]
    with SiteDB(home) as db:
        (after,) = db.tracks(KEY)
        assert after.pinned == before.pinned


def test_widening_a_held_repository_stays_held(widened):
    root, home, bundle = widened
    ops.hold(home, KEY)
    result = apply_bundle(home, bundle("oak", "arm64"), say=_quiet)
    assert result.stored_held == [KEY]
    assert not (root / ARM).exists()
    assert (root / AMD).is_file()
    assert result.updated == []
    with SiteDB(home) as db:
        assert db.repo_state(KEY).held
