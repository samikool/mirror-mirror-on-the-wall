"""Operations on a set of roots: each repository's own root does the work."""
import pytest

from mmw.site import ops
from mmw.site import recents
from mmw.site.apply import apply_to_set
from mmw.site.db import SiteDB
from mmw.site.home import load_site, read_root
from mmw.site.mset import open_set

from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture
from test_site_apply import _bundle
from test_site_set_apply import HOST_A, HOST_B, hosts  # noqa: F401
from test_snapshot_repo_versions import _change_a

quiet = lambda *a: None  # noqa: E731


def _split(hosts, tmp_path):  # noqa: F811
    cfg, net, md = hosts
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)),
                 place={HOST_A: str(r1), HOST_B: str(r2)}, say=quiet)
    return cfg, net, md, r1, r2


def test_set_status_spans_roots(hosts, tmp_path):  # noqa: F811
    *_, r1, r2 = _split(hosts, tmp_path)
    rows = ops.set_status(open_set())
    assert sorted((root, st.label) for root, st in rows) == [("r1", "a"), ("r2", "b")]


def test_serve_routes_to_the_owning_root(hosts, tmp_path):  # noqa: F811
    cfg, net, md, r1, r2 = _split(hosts, tmp_path)
    _change_a(md)
    apply_to_set(open_set(), str(_bundle(cfg, net, md, tmp_path / "2")), say=quiet)
    root, key = ops.on_repo(open_set(), "a")
    assert root.info.name == "r1"
    root.serve(key, 1)
    with SiteDB(read_root(str(r1)).home) as db:
        st = db.repo_state(key)
        assert st.serving.number == 1 and st.held


def test_move_repo_between_roots(hosts, tmp_path):  # noqa: F811
    from test_site_apply import _tree
    cfg, net, md, r1, r2 = _split(hosts, tmp_path)
    served_b = {p: v for p, v in _tree(r2).items() if p.startswith(HOST_B)}
    ops.move_repo(open_set(), "b", "r1")
    assert {p: v for p, v in _tree(r1).items() if p.startswith(HOST_B)} == served_b
    assert {p for p in _tree(r2) if p.startswith(HOST_B)}         # the old copy stays until tidy
    assert sorted((root, st.label) for root, st in ops.set_status(open_set())) == [("r1", "a"), ("r1", "b")]
    assert load_site(read_root(str(r2)).home).hosts[HOST_B] == "r1"
    assert ops.tidy_set(open_set()) == ["b"]
    assert not {p for p in _tree(r2) if p.startswith(HOST_B)}


def test_move_refuses_splitting_a_hostname(two_mirrors, tmp_path):  # noqa: F811
    from test_bundle_mmw import _mmw
    cfg, net, md = _mmw(two_mirrors)                               # a and b: one hostname
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)),
                 place={"*": str(r1)}, say=quiet)
    from mmw.site.home import init_root
    m = open_set()
    init_root(r2, name="r2", set_id=m.set_id, network=m.network)
    recents.remember(str(r2))
    with pytest.raises(ValueError, match="share"):
        ops.move_repo(open_set(), "a", "r2")


def test_ca_location_lives_on_this_machine(hosts, tmp_path):  # noqa: F811
    from mmw.site import certs as C
    *_, r1, r2 = _split(hosts, tmp_path)
    crt, key = C.make_ca(tmp_path / "ca", "Lab CA", None)
    C.load_ca(crt, key, None)
    assert recents.ca_paths() == (crt.resolve(), key.resolve())
    for r in (r1, r2):
        assert "ca_cert" not in (read_root(str(r)).home / "site.toml").read_text()
    signed = C.sign_set(open_set(), password=None)
    assert sorted(x.root for x in signed) == ["r1", "r2"]
    assert (r2 / ".mmw" / "tls" / "mirror.crt").exists()


def test_a_move_cut_short_runs_again(hosts, tmp_path, monkeypatch):  # noqa: F811
    cfg, net, md, r1, r2 = _split(hosts, tmp_path)
    real = SiteDB.drop_repo
    monkeypatch.setattr(SiteDB, "drop_repo", lambda self, repo: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        ops.move_repo(open_set(), "b", "r1")
    recents.remember(str(r1))                                   # the destination now first in order
    assert open_set().owner(HOST_B).info.name == "r2"           # the partial copy never takes the hostname
    monkeypatch.setattr(SiteDB, "drop_repo", real)
    ops.move_repo(open_set(), "b", "r1")
    assert sorted((root, st.label) for root, st in ops.set_status(open_set())) == [("r1", "a"), ("r1", "b")]


def test_move_checks_space_first(hosts, tmp_path, monkeypatch):  # noqa: F811
    from mmw.site.mset import LocalRoot
    cfg, net, md, r1, r2 = _split(hosts, tmp_path)
    monkeypatch.setattr(LocalRoot, "free_bytes", lambda self: 1)
    with pytest.raises(ValueError, match="free"):
        ops.move_repo(open_set(), "b", "r1")
    assert [label for root, st in ops.set_status(open_set()) for label in [st.label] if root == "r1"] == ["a"]
