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


# ── building an overlay (Task 3) ─────────────────────────────────────────────

import shutil  # noqa: E402

import pytest  # noqa: E402

from debs import make_deb  # noqa: E402
from mmw.core import config  # noqa: E402
from mmw.core.apt_index import parse_packages, release_body  # noqa: E402
from mmw.core.signing import Keyring  # noqa: E402
from test_layers import lab  # noqa: E402,F401 — the fixture
from test_signing import gpgv_ok  # noqa: E402

needs_gpg = pytest.mark.skipif(shutil.which("gpg") is None or shutil.which("gpgv") is None,
                               reason="needs gpg and gpgv")


@pytest.fixture
def site(lab):  # noqa: F811
    """`lab`'s upstream bundled (far_side = mmw) and applied to a fresh site."""
    from mmw.core.publisher.bundle import bundle_network
    from mmw.site import home as H
    from mmw.site.apply import apply_bundle
    net = config.NetworkConfig(name="lab", root=lab.tmp / "pub", far_side="mmw")
    cfg = config.Config(settings=config.Settings(mirror_dir=lab.mirror_dir), sources=[lab.src], networks=[net])
    home = H.init_root(lab.tmp / "srv", name="main", set_id="s1", network="lab")
    out = bundle_network(net, cfg, lab.mirror_dir, out_path=lab.tmp / "b1", from_ref=None).out_path
    apply_bundle(home, str(out), say=lambda *a: None)
    with SiteDB(home) as db:
        [state] = db.repo_states()
    Keyring(home).generate("site")

    class Site:
        pass
    s = Site()
    s.lab, s.cfg, s.net, s.home, s.root, s.repo, s.base = lab, cfg, net, home, lab.tmp / "srv", state.repo, state.bases[0]
    s.folder = site_layer_dir(home, state.repo)

    def build():
        from mmw.site.layers import build_overlay
        from mmw.site.roots import Roots
        from mmw.site.home import load_site
        roots = Roots(load_site(home))
        with SiteDB(home) as db:
            vid = db.repo_state(s.repo).serving.id
            result = build_overlay(home, db, roots.store(roots.primary), s.repo, vid)
            files = db.overlay_files(s.repo, vid, result.digest) if result.digest else {}
        return result, files, roots.store(roots.primary)
    s.build = build
    yield s
    Keyring(home).stop_agent()


@needs_gpg
def test_a_site_layer_overlays_the_served_version(site):
    make_deb(site.folder / "jammy/main", "site-tool", "1.0")
    with SiteDB(site.home) as db:
        db.set_layer(site.repo, signing="resign", key="site")
    result, files, store = site.build()
    assert result.hold_reason is None and result.digest
    b = site.base
    packages = parse_packages(store.path(files[f"{b}/dists/jammy/main/binary-amd64/Packages"]).read_text())
    assert [p["Package"] for p in packages] == ["hello", "site-tool"]
    assert f"{b}/pool/mmw/main/site-tool_1.0_amd64.deb" in files
    release = store.path(files[f"{b}/dists/jammy/Release"]).read_bytes()
    public = Keyring(site.home).public_armor("site")
    assert gpgv_ok(site.lab.tmp, public, store.path(files[f"{b}/dists/jammy/Release.gpg"]).read_bytes(), release)
    assert release_body(store.path(files[f"{b}/dists/jammy/InRelease"]).read_bytes()) == release
    assert f"{b}/mmw-keys/site.gpg" in files                         # clients fetch the site key


@needs_gpg
def test_holds_when_the_layer_cannot_be_signed(site):
    make_deb(site.folder / "jammy/main", "site-tool", "1.0")
    result, files, _ = site.build()                                   # no layer policy: upstream
    assert result.digest is None and "resign or unsigned" in result.hold_reason
    with SiteDB(site.home) as db:
        db.set_layer(site.repo, signing="resign", key="nope")
    result, _, _ = site.build()
    assert result.digest is None and "signing key" in result.hold_reason


@needs_gpg
def test_no_debs_and_upstream_is_the_pure_version(site):
    result, files, _ = site.build()
    assert result.digest is None and result.hold_reason is None and files == {}


@needs_gpg
def test_an_unchanged_overlay_is_reused(site, monkeypatch):
    make_deb(site.folder / "jammy/main", "site-tool", "1.0")
    with SiteDB(site.home) as db:
        db.set_layer(site.repo, signing="unsigned")
    first, _, _ = site.build()
    from mmw.core import layers as core_layers
    monkeypatch.setattr(core_layers, "_rebuild", lambda *a, **k: pytest.fail("rebuilt"))
    again, _, _ = site.build()
    assert again.digest == first.digest
