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
