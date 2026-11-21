"""A far side apply.py manages, converted to mmw from the tree it has: its
manifest says every file; APT bases become repositories, the rest the
network's files; the network's first mmw bundle claims what was adopted."""
import pytest

from mmw.core.publisher.bundle import bundle_network
from mmw.site.adopt import adopt_apply_tree, apt_bases, find_apply_tree
from mmw.site.db import SiteDB

from test_bundle_snapshots import _tree, states  # noqa: F401

BASE = "archive.ubuntu.com/ubuntu"


@pytest.fixture
def far(states, tmp_path):  # noqa: F811
    """A far side as apply.py left it at snapshot s3 — the tree and its
    `.mmw/manifest.jsonl` record, which a publish keeps the same way."""
    from mmw.core.publisher import publish_snapshot
    cfg, net, md, snaps, published = states
    dest = tmp_path / "far"
    assert not publish_snapshot(net, cfg, md, "s3", root=dest).errors
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


def test_the_first_bundle_never_unserves_adopted_files_its_repositories_hold(far, tmp_path, monkeypatch):
    """Adopted as the network's (nothing claimed it yet), now a repository's:
    the network-file step leaves it for that repository's switch — which
    here doesn't happen (held) — so it is still served."""
    import hashlib
    import json
    import tarfile

    from mmw.site import apply as A
    from mmw.site.home import root_home
    from mmw.site.switch import SwitchResult
    from test_bundle_snapshots import _state
    cfg, net, md, snaps, published, dest = far
    adopt_apply_tree(find_apply_tree(dest))
    _state(md, 4)
    from mmw.core import snapshots
    monkeypatch.setattr(snapshots, "now_stamp", lambda: "20991231T000000Z")
    r = _mmw_bundle(cfg, net, md, tmp_path / "m.tar")
    with tarfile.open(r.out_path) as tar:
        rows = [json.loads(ln) for ln in tar.extractfile(".mmw/manifest.jsonl").read().decode().splitlines()[1:]]
    kept = next(row["p"] for row in rows if row.get("r") and (dest / row["p"]).is_file())   # here, and still upstream's
    with SiteDB(root_home(dest)) as db:                       # as if adoption couldn't place it
        files = db.network_files()
        files[kept] = hashlib.sha256((dest / kept).read_bytes()).hexdigest()
        db.set_network_files(files, {})
    monkeypatch.setattr(A, "switch_in", lambda *a, **k: SwitchResult(0, 0, held="its layer can't be built"))
    A.apply_bundle(root_home(dest), str(r.out_path), say=lambda *a: None)
    assert (dest / kept).exists()
    from mmw.site.apply import _served_by_repos
    with SiteDB(root_home(dest)) as db:                       # someone's: a later bundle can remove it
        assert kept in db.network_files() or _served_by_repos(db, {kept})


def test_a_flat_repository_in_a_subdirectory_is_claimed_by_its_repository():
    """apply.py's tree has `b.example/flat/amd64/Release`: adopted with base
    `b.example/flat/amd64`; the connected side's base is `b.example/flat`.
    Its first bundle claims it — and never one under another base."""
    from types import SimpleNamespace

    from mmw.site.apply import _claim_adopted
    renamed = []
    db = SimpleNamespace(
        repo_state=lambda repo: None,
        repo_states=lambda: [SimpleNamespace(repo="adopted:b.example/flat/amd64", bases=["b.example/flat/amd64"]),
                             SimpleNamespace(repo="adopted:b.example/flatter", bases=["b.example/flatter"])],
        rename_repo=lambda *a: renamed.append(a))
    result = SimpleNamespace(lines=[])
    _claim_adopted(db, "https://b.example/flat", {"label": "flat", "bases": ["b.example/flat"]}, result)
    assert [r[0] for r in renamed] == ["adopted:b.example/flat/amd64"]


def test_a_dists_at_the_trees_top_is_said(far):
    from mmw.site.adopt import ApplyTree
    *_x, dest = far
    tree = find_apply_tree(dest)
    rel = "dists/stable/Release"
    (dest / rel).parent.mkdir(parents=True)
    (dest / rel).write_bytes(b"Suite: stable\n")
    import hashlib
    tree = ApplyTree(**{**tree.__dict__, "rows": {**tree.rows, rel: hashlib.sha256(b"Suite: stable\n").hexdigest()}})
    r = adopt_apply_tree(tree)
    assert any("dists/ at the tree's top" in ln for ln in r.lines)


def test_adopting_an_apply_tree_says_its_signatures_were_not_checked_here(far):
    *_x, dest = far
    r = adopt_apply_tree(find_apply_tree(dest))
    assert any("signatures not checked here" in ln and "check-signatures" in ln for ln in r.lines)


def test_claiming_goes_to_the_longest_base_and_says_another_tree_under_it():
    from types import SimpleNamespace

    from mmw.site.apply import _claim_adopted
    renamed = []
    adopted = [SimpleNamespace(repo="adopted:h/debian", bases=["h/debian"]),
              SimpleNamespace(repo="adopted:h/a", bases=["h/a"]), SimpleNamespace(repo="adopted:h/b", bases=["h/b"])]
    db = SimpleNamespace(repo_state=lambda repo: None, repo_states=lambda: adopted,
                         rename_repo=lambda *a: renamed.append(a[0]))
    every = ["h", "h/debian"]
    host = SimpleNamespace(lines=[])
    _claim_adopted(db, "https://h", {"label": "host", "bases": ["h"]}, host, every)
    assert renamed == ["adopted:h/a"]                                 # never h/debian: its own repository's
    assert any("adopted at h/b lies under it" in ln for ln in host.lines)
    deb = SimpleNamespace(lines=[])
    _claim_adopted(db, "https://h/debian", {"label": "Debian", "bases": ["h/debian"]}, deb, every)
    assert renamed[-1] == "adopted:h/debian"


def test_a_network_file_a_repository_will_hold_stays_recorded_until_it_does(far, tmp_path, monkeypatch):
    """No repository serves it yet and this one's switch doesn't happen: it is
    still the network's — served and recorded, so a later bundle can remove it."""
    import hashlib
    import json
    import tarfile

    from mmw.site import apply as A
    from mmw.site.home import root_home
    from mmw.site.switch import SwitchResult
    from test_bundle_snapshots import _state
    cfg, net, md, snaps, published, dest = far
    adopt_apply_tree(find_apply_tree(dest))
    with SiteDB(root_home(dest)) as db:
        db.drop_repo(f"adopted:{BASE}")                       # nothing serves the tree's files any more
    _state(md, 4)
    from mmw.core import snapshots
    monkeypatch.setattr(snapshots, "now_stamp", lambda: "20991231T000000Z")
    r = _mmw_bundle(cfg, net, md, tmp_path / "m.tar")
    with tarfile.open(r.out_path) as tar:
        rows = [json.loads(ln) for ln in tar.extractfile(".mmw/manifest.jsonl").read().decode().splitlines()[1:]]
    kept = next(row["p"] for row in rows if row.get("r") and (dest / row["p"]).is_file())
    with SiteDB(root_home(dest)) as db:
        files = db.network_files()
        files[kept] = hashlib.sha256((dest / kept).read_bytes()).hexdigest()
        db.set_network_files(files, {})
    monkeypatch.setattr(A, "switch_in", lambda *a, **k: SwitchResult(0, 0, held="its layer can't be built"))
    A.apply_bundle(root_home(dest), str(r.out_path), say=lambda *a: None)
    assert (dest / kept).exists()
    with SiteDB(root_home(dest)) as db:
        assert kept in db.network_files()


def test_apply_tree_then_foreign_bundle_claims_it(far, tmp_path):
    """A tree apply.py kept for network 'lab', then the same repository from network 'other'."""
    from mmw.site.apply import apply_to_set
    from mmw.site.home import root_home
    from mmw.site.mset import open_set
    cfg, net, md, snaps, published, dest = far
    adopt_apply_tree(find_apply_tree(dest))
    other = net.model_copy(update={"name": "other"})
    cfg.networks.append(other)
    b = str(bundle_network(other, cfg, md, out_path=tmp_path / "o").out_path)
    r = apply_to_set(open_set([str(dest)]), b, say=lambda *_: None)
    lines = [line.strip() for line in r.merged().lines]
    assert not any("not mirrored here" in line for line in lines), lines
    with SiteDB(root_home(dest)) as db:
        (st,) = db.repo_states()
        assert not st.repo.startswith("adopted:") and st.bases == [BASE]
    r = apply_to_set(open_set([str(dest)]), b, say=lambda *_: None, deploy={"ubuntu-archive"})
    with SiteDB(root_home(dest)) as db:
        assert len(db.repo_states()) == 1
