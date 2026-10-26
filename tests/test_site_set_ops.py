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


def _one_hostname_two_roots(two_mirrors, tmp_path):  # noqa: F811
    from test_bundle_mmw import _mmw
    cfg, net, md = _mmw(two_mirrors)                               # a and b: one hostname
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)),
                 place={"*": str(r1)}, say=quiet)
    from mmw.site.home import init_root
    m = open_set()
    init_root(r2, name="r2", set_id=m.set_id, network=m.network)
    recents.remember(str(r2))
    return r1, r2


def test_move_takes_the_whole_host(two_mirrors, tmp_path):  # noqa: F811
    _one_hostname_two_roots(two_mirrors, tmp_path)
    p = ops.move_preview(open_set(), "a", "r2")
    assert p.labels == ["a", "b"] and p.whole_host and len(p.hosts) == 1 and p.copies > 0
    r = ops.move_repo(open_set(), "a", "r2")
    assert r.moved == ["a", "b"] and r.copied_bytes == p.copies
    assert sorted((root, st.label) for root, st in ops.set_status(open_set())) == [("r2", "a"), ("r2", "b")]
    assert open_set().owner(p.hosts[0]).info.name == "r2"


def test_a_repository_alone_on_its_hostname_moves_alone(hosts, tmp_path):  # noqa: F811
    _split(hosts, tmp_path)
    p = ops.move_preview(open_set(), "b", "r1")
    assert p.labels == ["b"] and not p.whole_host


def test_move_says_how_it_goes(hosts, tmp_path):  # noqa: F811
    _split(hosts, tmp_path)
    events = []
    ops.move_repo(open_set(), "b", "r1", progress=lambda event, **d: events.append((event, d)))
    kinds = [e for e, _d in events]
    assert "copy" in kinds and kinds[-2:] == ["switch", "done"]
    assert sum(d["bytes"] for e, d in events if e == "copy") > 0


def test_a_stopped_move_switches_nothing_and_runs_again(hosts, tmp_path):  # noqa: F811
    import threading
    _split(hosts, tmp_path)
    stop = threading.Event()
    stop.set()
    with pytest.raises(ValueError, match="stopped"):
        ops.move_repo(open_set(), "b", "r1", stop_event=stop)
    assert sorted((root, st.label) for root, st in ops.set_status(open_set())) == [("r1", "a"), ("r2", "b")]
    ops.move_repo(open_set(), "b", "r1")
    assert sorted((root, st.label) for root, st in ops.set_status(open_set())) == [("r1", "a"), ("r1", "b")]


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


def test_move_in_refuses_paths_outside_the_root(hosts, tmp_path):  # noqa: F811
    *_, r1, r2 = _split(hosts, tmp_path)
    from mmw.site import ops as O
    bad = {"label": "x", "bases": ["example.net/x"], "held": False, "serving": 1, "hosts": ["example.net"],
           "sharing": [], "versions": [{"number": 1, "label": "x", "digest": "d", "stamp": "s", "snap_label": None,
                                         "files": [["../../escape", "ab" * 32, 1]]}]}
    with pytest.raises(ValueError, match="outside"):
        O.move_in(read_root(str(r1)).home, "kx", bad)


# ── adding a root from the TUI's Add root… ───────────────────────────────────

def test_add_root_makes_an_empty_root_of_this_mirror(hosts, tmp_path):  # noqa: F811
    *_, r1, r2 = _split(hosts, tmp_path)
    r3 = tmp_path / "r3"
    msg = ops.add_root(open_set(), str(r3))
    m = open_set()
    assert "r3" in [r.info.name for r in m.roots] and "new" in msg
    assert set(read_root(str(r1)).members) == {"r1", "r2", "r3"}


def test_add_root_opens_a_root_of_this_mirror(hosts, tmp_path):  # noqa: F811
    *_, r1, r2 = _split(hosts, tmp_path)
    recents.forget(str(r2))
    assert "opened" in ops.add_root(open_set(), str(r2))
    assert str(r2) in recents.known()


def test_add_root_refuses_another_mirror(hosts, tmp_path):  # noqa: F811
    from mmw.site.home import init_root
    _split(hosts, tmp_path)
    other = tmp_path / "other"
    init_root(other, name="other", set_id="elsewhere", network="elm")
    with pytest.raises(ValueError, match="elm"):
        ops.add_root(open_set(), str(other))


def test_add_root_again_finishes_a_root_whose_membership_was_half_written(hosts, tmp_path):  # noqa: F811
    from mmw.site.home import init_root
    *_, r1, r2 = _split(hosts, tmp_path)
    r3 = tmp_path / "r3"
    # Made, but the failure came before any root listed it, and before it was remembered.
    init_root(r3, name="r3", set_id=open_set().set_id, network=open_set().network)
    ops.add_root(open_set(), str(r3))
    for r in (r1, r2, r3):
        assert set(read_root(str(r)).members) == {"r1", "r2", "r3"}
    assert str(r3) in recents.known()


def test_add_root_refuses_a_folder_with_things_in_it(hosts, tmp_path):  # noqa: F811
    _split(hosts, tmp_path)
    busy = tmp_path / "home-dir"
    busy.mkdir()
    (busy / "notes.txt").write_text("mine")
    with pytest.raises(ValueError, match="isn't empty"):
        ops.add_root(open_set(), str(busy))
    assert not (busy / ".mmw").exists()


def test_add_root_takes_a_fresh_disk_with_only_lost_and_found(hosts, tmp_path):  # noqa: F811
    _split(hosts, tmp_path)
    disk = tmp_path / "disk"
    (disk / "lost+found").mkdir(parents=True)
    assert "new" in ops.add_root(open_set(), str(disk))


def test_add_root_remembers_a_typed_relative_path_in_full(hosts, tmp_path, monkeypatch):  # noqa: F811
    _split(hosts, tmp_path)
    monkeypatch.chdir(tmp_path)
    ops.add_root(open_set(), "r3")
    assert str(tmp_path / "r3") in recents.known() and "r3" not in recents.known()


def test_cli_move_says_the_whole_host_moves(two_mirrors, tmp_path):  # noqa: F811
    from typer.testing import CliRunner
    from mmw import cli
    _one_hostname_two_roots(two_mirrors, tmp_path)
    r = CliRunner().invoke(cli.app, ["site", "move", "a", "r2", "--yes"])
    assert r.exit_code == 0, r.output
    assert "can't move on its own: a, b move together" in r.output and "moved a, b" in r.output
