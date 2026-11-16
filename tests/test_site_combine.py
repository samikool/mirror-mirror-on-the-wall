"""Combining slices of one suite from different days: each point says which
slice it covers; combining writes a new Release, re-signed with a site key,
after saying so."""
import subprocess

import pytest
from typer.testing import CliRunner

from mmw import cli
from mmw.core.signing import Keyring
from mmw.site import home as H
from mmw.site.adopt import adopt_tree, check_tree, find_trees, keyring_from
from mmw.site.combine import combine, notes, selection
from mmw.site.db import SiteDB

from apt_tree import make_tree

BASE = "deb.debian.org/debian"


@pytest.fixture
def two_slices(tmp_path, monkeypatch):
    """A site with bookworm amd64 on the 1st and arm64 on the 2nd (newest)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    root = tmp_path / "srv"
    home = H.init_root(root, name="main", set_id="s1", network="oak")
    repo = None
    for day, arch in ((1, "arm64"), (2, "amd64")):
        src = make_tree(tmp_path / f"t{day}", day=day, arch=arch,
                        packages={f"hello_{day}.0_{arch}.deb": f"{arch}{day}".encode() * 9})
        # As a real mirror: the uncompressed index is listed in the Release, not held.
        (src / f"dists/bookworm/main/binary-{arch}/Packages").unlink()
        (tree,) = find_trees(src)
        repo = adopt_tree(home, tree, check_tree(tree, None), repo=repo, label="Debian", base=BASE).repo
    keys = Keyring(home)
    keys.generate("site")
    yield home, root.resolve(), repo, keys
    keys.stop_agent()


def _points(home, repo):
    with SiteDB(home) as db:
        (t,) = [t for t in db.tracks(repo) if t.name.endswith("dists/bookworm")]
        return t, db.points(t.id)


def test_selection_of_a_point(two_slices):
    home, _root, repo, _k = two_slices
    t, pts = _points(home, repo)
    with SiteDB(home) as db:
        assert selection(db.point_files(pts[0].id), t.name) == {"arches": ["amd64"], "components": ["main"]}
        assert selection(db.point_files(pts[1].id), t.name) == {"arches": ["arm64"], "components": ["main"]}


def test_history_says_slices_and_combined(two_slices):
    home, _root, repo, _k = two_slices
    with SiteDB(home) as db:
        said = notes(db, repo)
    t, pts = _points(home, repo)
    assert "amd64 · main" in said[pts[0].id]
    assert "has arm64; the newest doesn't" in said[pts[1].id]


def test_combined_serves_both_slices(two_slices):
    home, root, repo, _k = two_slices
    r = combine(home, repo, "bookworm", ["2026-11-01", "2026-11-02"], "site")
    rel = root / BASE / "dists/bookworm/Release"
    text = rel.read_text()
    assert "main/binary-amd64/Packages" in text and "main/binary-arm64/Packages" in text
    assert "Architectures: amd64 arm64" in text and "Acquire-By-Hash" not in text
    for arch, day in (("amd64", 2), ("arm64", 1)):
        assert (root / BASE / f"dists/bookworm/main/binary-{arch}/Packages.gz").exists()
        assert (root / BASE / f"pool/main/h/hello/hello_{day}.0_{arch}.deb").exists()
    with SiteDB(home) as db:
        said = notes(db, repo)
    assert "mmw-signed" in said[r.point] and "2026-11-01" in said[r.point]


def test_combined_is_signed_with_the_site_key(two_slices, tmp_path):
    home, root, repo, keys = two_slices
    combine(home, repo, "bookworm", ["2026-11-01", "2026-11-02"], "site")
    ring = keyring_from([keys.public_armor("site").encode()], tmp_path / "site.gpg")
    ok = subprocess.run(["gpgv", "--keyring", str(ring), str(root / BASE / "dists/bookworm/InRelease")],
                        capture_output=True)
    assert ok.returncode == 0
    assert (root / BASE / "mmw-keys/site.gpg").exists()


def test_no_such_key_is_refused(two_slices):
    home, _root, repo, _k = two_slices
    with pytest.raises(ValueError, match="mmw site signing-key generate"):
        combine(home, repo, "bookworm", ["2026-11-01", "2026-11-02"], "nope")


def test_one_point_is_not_a_combination(two_slices):
    home, _root, repo, _k = two_slices
    with pytest.raises(ValueError, match="two or more"):
        combine(home, repo, "bookworm", ["2026-11-02", "2026-11-02"], "site")


def test_cli_combine_warns_and_needs_yes(two_slices):
    home, root, repo, _k = two_slices
    args = ["site", "combine", repo, "--track", "bookworm", "2026-11-01", "2026-11-02", "--key", "site",
            "--root", str(root)]
    r = CliRunner().invoke(cli.app, args)
    assert r.exit_code == 1 and "no longer signed by upstream" in r.output and "--yes" in r.output
    r = CliRunner().invoke(cli.app, [*args, "--yes"])
    assert r.exit_code == 0, r.output
    assert "mmw-keys/site.gpg" in r.output


def _entries(text: str) -> dict[str, tuple[str, int]]:
    out, inside = {}, False
    for ln in text.splitlines():
        if not ln[:1].isspace():
            inside = ln.strip() == "SHA256:"
            continue
        parts = ln.split()
        if inside and len(parts) == 3:
            out[parts[2]] = (parts[0], int(parts[1]))
    return out


def test_every_hash_the_release_lists_is_right(two_slices):
    """What apt checks: each listed file (or, for an uncompressed index it
    doesn't hold, the decompressed variant) has exactly the listed hash."""
    import gzip
    import hashlib
    home, root, repo, _k = two_slices
    combine(home, repo, "bookworm", ["2026-11-01", "2026-11-02"], "site")
    suite = root / BASE / "dists/bookworm"
    entries = _entries((suite / "Release").read_text())
    assert {"main/binary-amd64/Packages", "main/binary-arm64/Packages"} <= set(entries)
    for path, (sha, size) in entries.items():
        f = suite / path
        data = f.read_bytes() if f.exists() else gzip.decompress((suite / (path + ".gz")).read_bytes())
        assert (hashlib.sha256(data).hexdigest(), len(data)) == (sha, size), path


def test_the_release_keeps_upstreams_date_and_drops_valid_until(two_slices):
    home, root, repo, _k = two_slices
    combine(home, repo, "bookworm", ["2026-11-01", "2026-11-02"], "site")
    text = (root / BASE / "dists/bookworm/Release").read_text()
    assert "Date: Mon, 02 Nov 2026" in text or "02 Nov 2026" in text
    assert "Valid-Until" not in text and "Signed-By" not in text


def test_a_pinned_suite_says_where_the_combined_point_is(two_slices):
    from mmw.site import ops
    home, root, repo, _k = two_slices
    ops.serve(home, repo, "2026-11-01")
    r = combine(home, repo, "bookworm", ["2026-11-01", "2026-11-02"], "site")
    assert any("pinned" in line and "mmw site serve" in line for line in r.lines)


def test_a_flat_repository_has_nothing_to_combine(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    home = H.init_root(tmp_path / "srv", name="main", set_id="s1", network="oak")
    repo = None
    for day in (1, 2):
        src = make_tree(tmp_path / f"f{day}", suite="tools", flat=True, day=day,
                        packages={f"t{day}_1_all.deb": bytes([day]) * 9})
        (tree,) = find_trees(src)
        repo = adopt_tree(home, tree, check_tree(tree, None), repo=repo, label="Tools",
                          base="tools.example/apt").repo
    keys = Keyring(home)
    keys.generate("site")
    try:
        with SiteDB(home) as db:
            (t,) = [t for t in db.tracks(repo) if t.name]
        with pytest.raises(ValueError, match="flat"):
            combine(home, repo, "tools", ["2026-11-01", "2026-11-02"], "site")
    finally:
        keys.stop_agent()


def test_notes_compare_with_what_is_served_and_read_files_once(two_slices, monkeypatch):
    """Serving the older (arm64) day: the newer one is what has something
    the served one lacks. A second look reads no file lists."""
    from mmw.site import ops
    home, _root, repo, _k = two_slices
    t, pts = _points(home, repo)
    ops.serve(home, repo, pts[1].date[:8], track=t.name)
    with SiteDB(home) as db:
        said = notes(db, repo)
    assert "has amd64; what is served doesn't" in said[pts[0].id]
    monkeypatch.setattr(SiteDB, "point_files", lambda *a, **k: pytest.fail("read again"))
    with SiteDB(home) as db:
        notes(db, repo)


def test_installer_indexes_go_with_their_architecture():
    """They add no architecture of their own (apt would look for its main
    index); combining takes them from the same day as that architecture."""
    files = {"dists/sid/main/binary-amd64/Packages": "a",
             "dists/sid/main/debian-installer/binary-arm64/Packages": "b"}
    assert selection(files, "dists/sid") == {"arches": ["amd64"], "components": ["main"]}


def test_a_refused_combination_leaves_nothing_in_the_store(two_slices, monkeypatch):
    from mmw.site import timeline
    home, root, repo, _keys = two_slices
    t, pts = _points(home, repo)
    before = {p.name for p in (root / ".mmw" / "store").rglob("*") if p.is_file()}

    def refuse(*a, **k):
        raise ValueError("the combined point was refused: a test")
    monkeypatch.setattr(timeline, "import_points", refuse)
    with pytest.raises(ValueError, match="refused"):
        combine(home, repo, t.name, [pts[0].date[:8], pts[1].date[:8]], "site")
    assert {p.name for p in (root / ".mmw" / "store").rglob("*") if p.is_file()} == before


def test_combine_from_the_tui(two_slices):
    """Pick the older point, j: the warning, the days and the key are there;
    Combine serves both slices."""
    import asyncio

    from textual.widgets import DataTable

    from mmw.site import recents
    from waiting import until
    home, root, repo, _keys = two_slices
    recents.remember(str(root))

    async def go():
        from mmw.site.mset import open_set
        from mmw.tui.site.app import SiteApp
        from mmw.tui.site.home import CombinePage
        app = SiteApp(open_set())
        async with app.run_test(size=(120, 40)) as pilot:
            await until(pilot, lambda: type(app.screen).__name__ == "SiteHomePage")
            t = app.screen.query_one("#site-history", DataTable)
            await until(pilot, lambda: t.row_count >= 3)
            page = app.screen
            t.move_cursor(row=next(i for i, (_t, p, *_x) in enumerate(page._history)
                                   if p is not None and p.date.startswith("20261101")))
            await pilot.press("j")
            await until(pilot, lambda: isinstance(app.screen, CombinePage))
            values = app.screen.query_one("#combine-days").value
            assert "2026-11-01" in values and "2026-11-02" in values
            assert app.screen.query_one("#combine-key").value == "site"
            assert "no longer signed by upstream" in str(app.screen.query_one("#combine-warning").render())
            app.screen.query_one("#btn-combine-go").press()
            await until(pilot, lambda: app.screen is page
                        and "combined" in str(page.query_one("#page-status").render()))
    asyncio.run(go())
    with SiteDB(home) as db:
        (t,) = [t for t in db.tracks(repo) if t.name]
        assert db.points(t.id)[0].ident.startswith("combined:")


def test_a_point_that_gains_a_slice_says_it(two_slices):
    """Its slice was worked out (amd64); the same Release arrives with arm64
    too: history says the new slice, not the cached one."""
    from mmw.site.tracks import PointDraft
    home, _root, repo, _k = two_slices
    t, pts = _points(home, repo)
    with SiteDB(home) as db:
        notes(db, repo)                                               # cached now
        newest, older = pts[0], pts[1]
        files = db.point_files(newest.id)
        extra = {p.replace("binary-amd64", "binary-arm64"): cs for p, cs in db.point_files(older.id).items()
                 if "/binary-arm64/" in p}
        status = db.import_point(t.id, PointDraft(t.name, newest.ident, newest.date, {}, {**files, **extra}),
                                 {}, newest.stamp, None)[1]
        assert status == "widened"
        said = notes(db, repo)
    assert "amd64,arm64" in said[newest.id]
