"""A far side apply.py manages, converted to mmw from the tree it has: its
manifest says every file; APT bases become repositories, the rest the
network's files; the network's first mmw bundle claims what was adopted."""
import pytest

from mmw.core.publisher.bundle import bundle_network
from mmw.site.adopt import adopt_apply_tree, apt_bases, find_apply_tree
from mmw.site.db import SiteDB

from test_bundle_snapshots import _apply, _tree, states  # noqa: F401

BASE = "archive.ubuntu.com/ubuntu"


@pytest.fixture
def far(states, tmp_path):  # noqa: F811
    """A far side apply.py brought to snapshot s3."""
    cfg, net, md, snaps, published = states
    r = bundle_network(net, cfg, md, from_ref=None, to_ref="s3")
    dest = tmp_path / "far"
    assert _apply(tmp_path, r, dest).returncode == 0
    return cfg, net, md, snaps, published, dest


def test_finds_the_manifest(far):
    *_x, dest = far
    tree = find_apply_tree(dest)
    assert tree.network == "lab" and tree.rows and all(not p.startswith(".mmw/") for p in tree.rows)
    assert find_apply_tree(dest / "archive.ubuntu.com") is None


def test_apt_bases_from_rows():
    rows = {"a.example/debian/dists/bookworm/Release": "x", "a.example/debian/pool/x.deb": "y",
            "b.example/flat/Release": "z", "b.example/flat/Packages": "w",
            "vscode-gallery/x.json": "v", "keys/a/k.gpg": "k"}
    assert apt_bases(rows) == ["a.example/debian", "b.example/flat"]


def test_in_place_keeps_every_served_file(far):
    *_x, published, dest = far
    before = {p: (dest / p).stat().st_ino for p in _tree(dest)}
    r = adopt_apply_tree(find_apply_tree(dest))
    assert not r.refused
    assert _tree(dest) == published[3]
    assert {p: (dest / p).stat().st_ino for p in _tree(dest)} == before        # the same files, linked
    from mmw.site.home import root_home
    with SiteDB(root_home(dest)) as db:
        (st,) = db.repo_states()
        assert st.repo == f"adopted:{BASE}" and st.bases == [BASE] and st.serving is not None
        assert db.network_files()


def test_a_damaged_file_refuses_its_repository(far):
    *_x, dest = far
    deb = next(p for p in _tree(dest) if p.endswith(".deb"))
    (dest / deb).write_bytes(b"damaged")
    r = adopt_apply_tree(find_apply_tree(dest))
    assert BASE in r.refused and "differs" in r.refused[BASE]


def test_into_another_root(far, tmp_path):
    from mmw.site import home as H
    *_x, published, dest = far
    home = H.init_root(tmp_path / "srv", name="main", set_id="s1", network="lab")
    r = adopt_apply_tree(find_apply_tree(dest), home=home)
    assert not r.refused
    assert _tree((tmp_path / "srv").resolve()) == published[3]


def test_cli_converts_an_apply_tree_in_place(far, tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from mmw import cli
    *_x, dest = far
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    r = CliRunner().invoke(cli.app, ["site", "adopt", str(dest), "--yes"])
    assert r.exit_code == 0, r.output
    assert "apply.py tree of lab" in r.output and BASE in r.output
    st = CliRunner().invoke(cli.app, ["site", "status", "--root", str(dest)])
    assert BASE in st.output, st.output


def _mmw_bundle(cfg, net, md, out):
    net = net.model_copy(update={"far_side": "mmw"})
    return bundle_network(net, cfg.model_copy(update={"networks": [net]}), md, out_path=out)


def test_an_apply_far_side_converts_in_place(far, tmp_path, monkeypatch):
    import tarfile

    from mmw.core.config import normalize_url
    from mmw.site.apply import apply_bundle
    from mmw.site.home import root_home
    from test_bundle_snapshots import SRC, _state
    cfg, net, md, snaps, published, dest = far
    adopt_apply_tree(find_apply_tree(dest))
    _state(md, 4)                                             # upstream moves on; the network becomes mmw
    from mmw.core import snapshots
    monkeypatch.setattr(snapshots, "now_stamp", lambda: "20991231T000000Z")   # later than s3's stamp
    r = _mmw_bundle(cfg, net, md, tmp_path / "m.tar")
    import hashlib

    from mmw.core.publisher.manifest import blob_member
    with tarfile.open(r.out_path) as tar:
        carried = set(tar.getnames())
    sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731
    assert blob_member(sha(b"hello 4")) in carried
    assert blob_member(sha(b"hello 3")) not in carried         # the far side has it: not sent again
    apply_bundle(root_home(dest), str(r.out_path), say=lambda *a: None)
    with SiteDB(root_home(dest)) as db:
        assert [s.repo for s in db.repo_states()] == [normalize_url(SRC)]
        (st,) = db.repo_states()
        assert st.bases == [BASE] and st.serving is not None
    assert (dest / BASE / "pool/main/h/hello/hello_4.0_amd64.deb").exists()


def test_a_bundle_claims_only_matching_bases(far, tmp_path):
    from mmw.site.home import root_home
    *_x, dest = far
    adopt_apply_tree(find_apply_tree(dest))
    with SiteDB(root_home(dest)) as db:
        db.rename_repo(f"adopted:{BASE}", "https://archive.ubuntu.com/ubuntu", "Ubuntu", [BASE])
        assert [s.repo for s in db.repo_states()] == ["https://archive.ubuntu.com/ubuntu"]
        (track,) = [t for t in db.tracks("https://archive.ubuntu.com/ubuntu") if t.name]
        assert db.points(track.id)
