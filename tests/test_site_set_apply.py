"""A bundle applied to a set of roots in one pass, routed by hostname."""
import json
import tarfile
import threading

import pytest

from mmw.site import recents
from mmw.site.apply import ApplyError, apply_to_set, plan_set_apply
from mmw.site.db import SiteDB
from mmw.site.home import read_root
from mmw.site.mset import LocalRoot, open_set

from test_bundle_mmw import _change_b, _mmw
from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture
from test_site_apply import _bundle
from test_snapshot_repo_versions import _change_a

quiet = lambda *a: None  # noqa: E731
HOST_A, HOST_B = "example.com", "example.org"


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture
def hosts(two_mirrors):  # noqa: F811
    """Repository a under example.com, b under example.org."""
    cfg, net, md = _mmw(two_mirrors)
    b = cfg.sources[1].model_copy(update={"url": "https://example.org/b/"})
    return cfg.model_copy(update={"sources": [cfg.sources[0], b]}), net, md


def _repos(root):
    with SiteDB(read_root(str(root)).home) as db:
        return {s.label for s in db.repo_states()}


def test_first_apply_places_every_host(hosts, tmp_path):
    cfg, net, md = hosts
    r1 = tmp_path / "r1"
    r = apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)),
                     place={"*": str(r1)}, say=quiet)
    info = read_root(str(r1))
    assert info.network == net.name and str(r1) in recents.known()
    assert _repos(r1) == {"a", "b"} and not r.held_back


def test_split_across_two_roots(hosts, tmp_path):
    cfg, net, md = hosts
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)),
                 place={HOST_A: str(r1), HOST_B: str(r2)}, say=quiet)
    assert (_repos(r1), _repos(r2)) == ({"a"}, {"b"})
    i1, i2 = read_root(str(r1)), read_root(str(r2))
    assert i1.set_id == i2.set_id and set(i1.members) == set(i2.members) == {i1.name, i2.name}
    for r in (r1, r2):
        assert (r / "nginx").is_dir()                             # network files in every root


def test_next_bundle_routes_itself(hosts, tmp_path):
    cfg, net, md = hosts
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)),
                 place={HOST_A: str(r1), HOST_B: str(r2)}, say=quiet)
    _change_a(md)
    _change_b(md)
    r = apply_to_set(open_set(), str(_bundle(cfg, net, md, tmp_path / "2")), say=quiet)
    assert sorted(len(x.updated) for x in r.per_root.values()) == [1, 1]


def test_new_host_without_place_is_an_error(hosts, tmp_path):
    cfg, net, md = hosts
    with pytest.raises(ApplyError, match=HOST_A):
        apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), say=quiet)
    assert not list(tmp_path.glob("r*"))


def test_missing_root_holds_back_its_hostnames(hosts, tmp_path):
    cfg, net, md = hosts
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)),
                 place={HOST_A: str(r1), HOST_B: str(r2)}, say=quiet)
    name2 = read_root(str(r2)).name
    (r2 / ".mmw").rename(tmp_path / "aside")                     # not mounted
    _change_a(md)
    _change_b(md)
    r = apply_to_set(open_set(), str(_bundle(cfg, net, md, tmp_path / "2")), say=quiet)
    assert name2 in r.held_back[HOST_B] and len(next(iter(r.per_root.values())).updated) == 1
    assert not (r2 / ".mmw").exists()                             # nothing written where it isn't


def test_space_checked_per_root_before_writing(hosts, tmp_path, monkeypatch):
    cfg, net, md = hosts
    r1 = tmp_path / "r1"
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)),
                 place={"*": str(r1)}, say=quiet)
    _change_a(md)
    monkeypatch.setattr(LocalRoot, "free_bytes", lambda self: 10)
    blobs = set((r1 / ".mmw" / "store").rglob("*"))
    with pytest.raises(ApplyError, match=r"needs .* free") as e:
        apply_to_set(open_set(), str(_bundle(cfg, net, md, tmp_path / "2")), say=quiet)
    assert read_root(str(r1)).name in str(e.value) and e.value.code == 5
    assert set((r1 / ".mmw" / "store").rglob("*")) == blobs


def test_bundle_from_another_network_is_refused(hosts, tmp_path):
    cfg, net, md = hosts
    r1 = tmp_path / "r1"
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)),
                 place={"*": str(r1)}, say=quiet)
    other = net.model_copy(update={"name": "elm"})
    cfg2 = cfg.model_copy(update={"networks": [other]})
    with pytest.raises(ApplyError, match="elm.*oak|oak.*elm") as e:
        apply_to_set(open_set(), str(_bundle(cfg2, other, md, tmp_path / "x", from_ref=None)), say=quiet)
    assert e.value.code == 5


def test_stop_never_half_switches_a_hostname(hosts, tmp_path):
    cfg, net, md = hosts
    r1 = tmp_path / "r1"
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)),
                 place={"*": str(r1)}, say=quiet)
    _change_a(md)
    _change_b(md)
    stop = threading.Event()

    def progress(event, **kw):
        if event == "switch":
            stop.set()                          # as the first hostname starts switching
    r = apply_to_set(open_set(), str(_bundle(cfg, net, md, tmp_path / "2")), say=quiet,
                     progress=progress, stop_event=stop)
    (res,) = r.per_root.values()
    assert len(res.updated) == 1 and len(res.stopped) == 1
    assert any("stopped" in line for line in r.lines)


def test_old_manifest_without_sizes_still_plans(hosts, tmp_path):
    cfg, net, md = hosts
    b = _bundle(cfg, net, md, tmp_path / "1", from_ref=None)
    with tarfile.open(b) as t:
        members = [(m, t.extractfile(m).read() if m.isreg() else None) for m in t.getmembers()]
    old = tmp_path / "old.tar"
    with tarfile.open(old, "w") as t:
        for m, data in members:
            if m.name == ".mmw/manifest.jsonl":
                lines = data.decode().splitlines()
                rows = [json.dumps({k: v for k, v in json.loads(x).items() if k != "s"}) for x in lines[1:]]
                data = ("\n".join([lines[0], *rows]) + "\n").encode()
                m.size = len(data)
            import io
            t.addfile(m, io.BytesIO(data) if data is not None else None)
    plan = plan_set_apply(None, str(old))
    assert plan.sizes[HOST_A] > 0 and plan.sizes[HOST_B] > 0
    assert plan.hosts.keys() == {HOST_A, HOST_B} and plan.owner == {HOST_A: None, HOST_B: None}


def test_cli_first_apply_then_status(hosts, tmp_path):
    from typer.testing import CliRunner

    from mmw import cli
    cfg, net, md = hosts
    b = _bundle(cfg, net, md, tmp_path / "1", from_ref=None)
    r1 = tmp_path / "r1"
    r = CliRunner().invoke(cli.app, ["site", "apply", str(b), "--root", f"*={r1}"])
    assert r.exit_code == 0, r.output
    out = CliRunner().invoke(cli.app, ["site", "status"]).output
    assert "a " in out and "b " in out and "current" in out


def test_cli_open_and_roots(hosts, tmp_path):
    from typer.testing import CliRunner

    from mmw import cli
    cfg, net, md = hosts
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)),
                 place={HOST_A: str(r1), HOST_B: str(r2)}, say=quiet)
    recents.forget(str(r2))
    out = CliRunner().invoke(cli.app, ["site", "roots"]).output
    assert "r2" in out and "MISSING" in out
    assert CliRunner().invoke(cli.app, ["site", "open", str(tmp_path)]).exit_code == 1    # no mirror there
    assert CliRunner().invoke(cli.app, ["site", "open", str(r2)]).exit_code == 0
    out = CliRunner().invoke(cli.app, ["site", "roots"]).output
    assert "MISSING" not in out and HOST_B in out


# ── review findings ──────────────────────────────────────────────────────────

def _split(hosts, tmp_path):
    cfg, net, md = hosts
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)),
                 place={HOST_A: str(r1), HOST_B: str(r2)}, say=quiet)
    return cfg, net, md, r1, r2


def _serving(root, label):
    with SiteDB(read_root(str(root)).home) as db:
        return next(s.serving.number for s in db.repo_states() if s.label == label)


def _damage(bundle, host, out):
    """The same bundle with one of `host`'s files' bytes changed."""
    import io
    with tarfile.open(bundle) as t:
        members = [(m, t.extractfile(m).read() if m.isreg() else None) for m in t.getmembers()]
    done = False
    with tarfile.open(out, "w") as t:
        for m, data in members:
            if not done and m.isreg() and m.name.startswith(host + "/") and m.name.endswith(".deb"):
                data, done = bytes(x ^ 1 for x in data), True
            t.addfile(m, io.BytesIO(data) if data is not None else None)
    assert done
    return out


def test_damage_for_one_root_changes_no_root(hosts, tmp_path):
    cfg, net, md, r1, r2 = _split(hosts, tmp_path)
    _change_a(md)
    _change_b(md)
    bad = _damage(_bundle(cfg, net, md, tmp_path / "2"), HOST_B, tmp_path / "bad.tar")
    with pytest.raises(ApplyError):
        apply_to_set(open_set(), str(bad), say=quiet)
    assert (_serving(r1, "a"), _serving(r2, "b")) == (1, 1)          # checked everywhere before any switch


def test_stop_while_one_root_switches_holds_the_others(hosts, tmp_path):
    cfg, net, md, r1, r2 = _split(hosts, tmp_path)
    _change_a(md)
    _change_b(md)
    stop = threading.Event()
    r = apply_to_set(open_set(), str(_bundle(cfg, net, md, tmp_path / "2")), say=quiet,
                     progress=lambda event, **kw: event == "switch" and stop.set(), stop_event=stop)
    merged = r.merged()
    assert len(merged.updated) == 1 and len(merged.stopped) == 1      # recorded and held, not an error


def test_network_files_and_nginx_reach_every_root(hosts, tmp_path):
    """A bundle carrying only the second root's hostname still brings the
    first root its network files, and each root's nginx serves only its own."""
    from test_snapshot_repo_versions import _key
    cfg, net, md = hosts
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None, repos=[_key(cfg, "a")])),
                 place={"*": str(r1)}, say=quiet)
    apply_to_set(open_set(), str(_bundle(cfg, net, md, tmp_path / "2", from_ref=None, repos=[_key(cfg, "b")])),
                 place={HOST_B: str(r2)}, say=quiet)
    homes = [read_root(str(r)).home for r in (r1, r2)]
    with SiteDB(homes[0]) as a, SiteDB(homes[1]) as b:
        assert a.network_files() == b.network_files()
    conf1, conf2 = ((r / "nginx" / "mirror.conf").read_text() for r in (r1, r2))
    assert HOST_A in conf1 and HOST_B not in conf1
    assert HOST_B in conf2 and HOST_A not in conf2


def test_an_unmounted_root_is_never_made_again(hosts, tmp_path):
    cfg, net, md = hosts
    r1 = tmp_path / "r1"
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), place={"*": str(r1)}, say=quiet)
    (r1 / ".mmw").rename(tmp_path / "aside")                     # the share isn't mounted
    with pytest.raises(ApplyError, match="mounted") as e:
        apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "2", from_ref=None)),
                     place={"*": str(r1)}, say=quiet)
    assert e.value.code == 5 and not (r1 / ".mmw").exists()


def test_no_new_root_while_one_is_missing(hosts, tmp_path):
    """A new hostname can't start a new root while a root of the mirror is
    missing: it may be the one that hostname belongs in, or the mount point."""
    from test_snapshot_repo_versions import _key
    from mmw.site.home import load_site, save_settings
    cfg, net, md = hosts
    r1 = tmp_path / "r1"
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None, repos=[_key(cfg, "a")])),
                 place={"*": str(r1)}, say=quiet)
    home = read_root(str(r1)).home
    save_settings(home, load_site(home).model_copy(update={"members": ["r1", "nas"]}))
    with pytest.raises(ApplyError, match="nas") as e:
        apply_to_set(open_set(), str(_bundle(cfg, net, md, tmp_path / "2", from_ref=None, repos=[_key(cfg, "b")])),
                     place={HOST_B: str(tmp_path / "r3")}, say=quiet)
    assert e.value.code == 5 and not (tmp_path / "r3").exists()


def test_members_keep_their_order_while_one_is_missing(hosts, tmp_path):
    cfg, net, md, r1, r2 = _split(hosts, tmp_path)
    before = read_root(str(r1)).members
    (r2 / ".mmw").rename(tmp_path / "aside")
    _change_a(md)
    apply_to_set(open_set(), str(_bundle(cfg, net, md, tmp_path / "2")), say=quiet)
    assert read_root(str(r1)).members == before
