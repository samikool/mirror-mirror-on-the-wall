"""A track's retirement follows the networks that list it."""
import io
import sqlite3

import pytest
from site_apt_rows import Repo
from site_format3 import write_format3

from mmw.site import ops
from mmw.site.adopt import adopt_tree, check_tree, find_trees
from mmw.site.apply import apply_bundle, apply_to_set
from mmw.site.db import SiteDB
from mmw.site.home import init_root, load_site, save_settings
from mmw.site.mset import open_set
from mmw.site.roots import Roots
from mmw.site.switch import switch_in
from mmw.site.tracks import split_tracks

BASE = "ex.org/debian"
REPO = "debian"
TRIXIE = f"{BASE}/dists/trixie/Release"


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "srv"
    return init_root(root, name="main", set_id="s1", network="oak"), root


def _repository(suites, day):
    repo = Repo(BASE)
    for suite in suites:
        repo.suite(suite, f"{day:02d} Nov 2026 00:00:00 UTC", [])
    return repo


def _apply(site, tmp_path, network, day, *suites, selection=True, direct=False):
    home, root = site
    repo = _repository(suites, day)
    stamp = f"202611{day:02d}T000000Z"
    points = [(d, stamp, None) for d in split_tracks(repo.rows, [BASE], repo.read, stamp)]
    path = write_format3(tmp_path / f"{network}-{day}.tar", network, {REPO: ("Debian", [BASE], points)},
                         repo.blobs, created=stamp, selection=selection)
    if direct:
        return apply_bundle(home, path, say=lambda *a: None)
    return apply_to_set(open_set([str(root)]), path, place={"*": str(root)}, say=lambda *a: None)


def test_foreign_suite_survives_own_next_bundle(site, tmp_path):
    _apply(site, tmp_path, "oak", 1, "bookworm")
    _apply(site, tmp_path, "elm", 2, "trixie")
    assert (site[1] / TRIXIE).is_file()

    _apply(site, tmp_path, "oak", 3, "bookworm")

    assert (site[1] / TRIXIE).is_file()


def test_suite_retires_when_every_contributor_dropped_it(site, tmp_path):
    _apply(site, tmp_path, "oak", 1, "bookworm", "trixie")
    _apply(site, tmp_path, "elm", 2, "bookworm", "trixie")
    _apply(site, tmp_path, "oak", 3, "bookworm")
    assert (site[1] / TRIXIE).is_file()

    _apply(site, tmp_path, "elm", 4, "bookworm")

    assert not (site[1] / TRIXIE).exists()


def test_foreign_selection_can_retire_its_own_suite(site, tmp_path):
    _apply(site, tmp_path, "oak", 1, "bookworm")
    _apply(site, tmp_path, "elm", 2, "bookworm", "trixie")
    assert (site[1] / TRIXIE).is_file()

    _apply(site, tmp_path, "elm", 3, "bookworm")

    assert not (site[1] / TRIXIE).exists()


def test_adopted_suite_is_never_retired_by_a_bundle(site, tmp_path):
    repo = _repository(["trixie"], 1)
    original = tmp_path / "original"
    for path in repo.rows:
        dest = original / path.removeprefix(f"{BASE}/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(repo.read(path))
    (tree,) = find_trees(original)
    adopt_tree(site[0], tree, check_tree(tree, None), repo=REPO, label="Debian", base=BASE)
    assert (site[1] / TRIXIE).is_file()

    _apply(site, tmp_path, "oak", 2, "bookworm", direct=True)

    assert (site[1] / TRIXIE).is_file()


def test_upgrade_keeps_existing_gone_flags(site):
    home, _root = site
    with SiteDB(home) as db:
        gone = db.ensure_track(REPO, "trixie", {})
        present = db.ensure_track(REPO, "bookworm", {})
    with sqlite3.connect(home / "site.db") as conn:
        conn.execute("PRAGMA user_version=0")  # schema before per-network contributions
        conn.execute("DROP TABLE IF EXISTS track_networks")
        conn.execute("DELETE FROM meta WHERE key = 'track-networks'")
        conn.execute("UPDATE tracks SET gone = 1 WHERE id = ?", (gone,))

    with SiteDB(home) as db:
        assert db.track(gone).gone
        assert not db.track(present).gone
    with sqlite3.connect(home / "site.db") as conn:
        rows = conn.execute("SELECT track, network, present FROM track_networks ORDER BY track").fetchall()
    assert rows == [(gone, "oak", 0), (present, "oak", 1)]


def test_export_does_not_create_a_contribution(site, tmp_path):
    _apply(site, tmp_path, "oak", 1, "bookworm", "trixie")
    _apply(site, tmp_path, "elm", 2, "trixie", selection=None)
    _apply(site, tmp_path, "oak", 3, "bookworm")

    assert not (site[1] / TRIXIE).exists()


def test_move_preserves_track_contributions(site, tmp_path):
    _apply(site, tmp_path, "oak", 1, "bookworm", "trixie", "bullseye")
    _apply(site, tmp_path, "oak", 2, "bookworm", "trixie")
    other = tmp_path / "other"
    ops.add_root(open_set(), str(other))
    ops.move_repo(open_set(), REPO, "other")
    moved = other / ".mmw" / "site", other
    with SiteDB(moved[0]) as db:
        assert {t.suite for t in db.tracks(REPO) if t.gone} == {"bullseye"}

    _apply(moved, tmp_path, "oak", 3, "bookworm", direct=True)

    assert not (other / TRIXIE).exists()
    assert not (other / f"{BASE}/dists/bullseye/Release").exists()


def test_last_contributor_can_retire_the_last_suite(site, tmp_path):
    _apply(site, tmp_path, "oak", 1, "trixie")
    assert (site[1] / TRIXIE).is_file()

    _apply(site, tmp_path, "oak", 2)

    assert not (site[1] / TRIXIE).exists()


def test_upgrade_without_home_network_keeps_omitted_suite_retired(site, tmp_path):
    home, root = site
    _apply(site, tmp_path, "oak", 1, "bookworm", "trixie")
    _apply(site, tmp_path, "oak", 2, "bookworm")
    save_settings(home, load_site(home).model_copy(update={"network": None}))
    with sqlite3.connect(home / "site.db") as conn:
        conn.execute("PRAGMA user_version=0")  # schema before per-network contributions
        conn.execute("DROP TABLE track_networks")
        conn.execute("DELETE FROM meta WHERE key = 'track-networks'")

    with SiteDB(home) as db:
        assert next(t.gone for t in db.tracks(REPO) if t.suite == "trixie")
    _apply(site, tmp_path, "oak", 3, "bookworm", direct=True)
    assert not (root / TRIXIE).exists()

    _apply(site, tmp_path, "oak", 4, "bookworm", "trixie", direct=True)
    assert (root / TRIXIE).is_file()


@pytest.mark.parametrize("network", ["oak", None])
def test_timeline_upgrade_keeps_older_suite_retired(site, tmp_path, network):
    home, root = site
    save_settings(home, load_site(home).model_copy(update={"network": network}))
    roots = Roots(load_site(home))
    store = roots.store(roots.primary)
    with SiteDB(home) as db:
        for day, suites in enumerate((["bookworm", "trixie"], ["bookworm"]), 1):
            repo = _repository(suites, day)
            for checksum, data in repo.blobs.items():
                store.add_stream(io.BytesIO(data), expect=checksum)
            files = [(path, checksum, len(repo.blobs[checksum])) for path, checksum in repo.rows.items()]
            version = db.add_version(REPO, "Debian", day, str(day), f"202611{day:02d}T000000Z",
                                     None, [BASE], files)
        switch_in(db, roots, REPO, version)

    assert not (root / TRIXIE).exists()
    _apply(site, tmp_path, "oak", 3, "bookworm", selection=None, direct=True)

    assert not (root / TRIXIE).exists()
