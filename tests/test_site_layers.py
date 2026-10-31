"""Far-side layers: custom debs a site adds to a repository it serves."""
import sqlite3

from mmw.site.db import DB_FILE, SiteDB
from mmw.site.layers import site_layer_dir


def _db(tmp_path):
    db = SiteDB(tmp_path)
    vid = db.add_version("archive.ubuntu.com/ubuntu", "ubuntu", 3, "d3", "s3", None,
                         ["archive.ubuntu.com/ubuntu"],
                         [("archive.ubuntu.com/ubuntu/dists/jammy/Release", "a" * 64, 10),
                          ("archive.ubuntu.com/ubuntu/dists/jammy/InRelease", "b" * 64, 20)])
    return db, vid


def test_layer_policy_round_trip(tmp_path):
    db, _ = _db(tmp_path)
    assert db.layer("archive.ubuntu.com/ubuntu") is None
    db.set_layer("archive.ubuntu.com/ubuntu", signing="resign", key="site", contents=True)
    layer = db.layer("archive.ubuntu.com/ubuntu")
    assert (layer.signing, layer.key, layer.contents, layer.drop_valid_until) == ("resign", "site", True, False)
    db.drop_layer("archive.ubuntu.com/ubuntu")
    assert db.layer("archive.ubuntu.com/ubuntu") is None


def test_served_files_are_the_version_plus_its_overlay(tmp_path):
    db, vid = _db(tmp_path)
    repo = "archive.ubuntu.com/ubuntu"
    db.put_overlay(repo, vid, "o1", [(f"{repo}/dists/jammy/Release", "c" * 64, 11),
                                     (f"{repo}/dists/jammy/InRelease", "", 0),
                                     (f"{repo}/pool/mmw/main/x.deb", "d" * 64, 99)])
    assert db.served_files(vid, None) == db.version_files(vid)
    served = db.served_files(vid, "o1")
    assert served == {f"{repo}/dists/jammy/Release": "c" * 64, f"{repo}/pool/mmw/main/x.deb": "d" * 64}
    assert {"c" * 64, "d" * 64} <= db.referenced_checksums()          # GC keeps overlay blobs
    db.set_overlay(repo, "o1")
    assert db.repo_overlay(repo) == "o1"


def test_an_old_site_database_is_migrated(tmp_path):
    conn = sqlite3.connect(tmp_path / DB_FILE)
    conn.executescript("CREATE TABLE repos (repo TEXT PRIMARY KEY, label TEXT NOT NULL, bases TEXT NOT NULL "
                       "DEFAULT '[]', serving INTEGER, held INTEGER NOT NULL DEFAULT 0);"
                       "CREATE TABLE switches (repo TEXT PRIMARY KEY, from_version INTEGER, "
                       "to_version INTEGER NOT NULL, phase TEXT NOT NULL DEFAULT 'pkg');")
    conn.close()
    with SiteDB(tmp_path) as db:
        cols = {r[1] for r in db._conn.execute("PRAGMA table_info(repos)")}
        assert {"root", "overlay"} <= cols
        assert "overlay" in {r[1] for r in db._conn.execute("PRAGMA table_info(switches)")}


def test_site_layer_dir_is_safe_for_any_repository_key(tmp_path):
    folder = site_layer_dir(tmp_path, "archive.ubuntu.com/ubuntu")
    assert folder.parent == tmp_path / "layers" and "/" not in folder.name
