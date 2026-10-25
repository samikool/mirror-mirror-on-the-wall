"""The far side v2 minors, fixed at the end (docs/internals/…/far-side-v2-deferred-minors.md)."""
import os
import threading
from pathlib import Path

import pytest

from mmw.site import ops, recents
from mmw.site.apply import ApplyError, apply_to_set, plan_set_apply
from mmw.site.db import SiteDB
from mmw.site.home import read_root
from mmw.site.mset import open_set

from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture
from test_site_apply import _bundle
from test_site_set_apply import HOST_A, HOST_B, hosts  # noqa: F401
from test_snapshot_repo_versions import _change_a

quiet = lambda *a: None  # noqa: E731


def _split(hosts, tmp_path):  # noqa: F811
    cfg, net, md = hosts
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    r = apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)),
                     place={HOST_A: str(r1), HOST_B: str(r2)}, say=quiet)
    return cfg, net, md, r1, r2, r


# 3 — the newest hosts map wins, never an old root's stale one
def test_newest_hosts_map_wins(hosts, tmp_path):  # noqa: F811
    from mmw.site.home import load_site, save_settings
    cfg, net, md, r1, r2, _ = _split(hosts, tmp_path)
    h2 = read_root(str(r2)).home
    stale = load_site(h2)
    save_settings(h2, stale.model_copy(update={"hosts": {**stale.hosts, HOST_B: "r1"}, "hosts_at": "0"}))
    (r2 / ".mmw").rename(tmp_path / "aside")
    plan = plan_set_apply(open_set(), str(_bundle(cfg, net, md, tmp_path / "x", from_ref=None)))
    assert plan.owner[HOST_B] == "r2"


# 4 — a first apply never joins a root of another network
def test_first_apply_never_joins_another_network(hosts, tmp_path):  # noqa: F811
    from mmw.site.home import init_root
    cfg, net, md = hosts
    other = tmp_path / "other"
    init_root(other, name="other", set_id="elsewhere", network="elm")
    with pytest.raises(ApplyError, match="elm"):
        apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), place={"*": str(other)}, say=quiet)


# 5 — dry run says when it can't check space; one filesystem is counted once
def test_space_on_one_filesystem_is_summed(hosts, tmp_path, monkeypatch):  # noqa: F811
    from mmw.site.apply import placement_needs
    cfg, net, md = hosts
    b = str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None))
    plan = plan_set_apply(None, b)
    needs = placement_needs(None, plan, {HOST_A: str(tmp_path / "a"), HOST_B: str(tmp_path / "b")})
    from mmw.site.apply import shared_filesystem_short
    total = sum(n for n, _f in needs.values())
    free = next(iter(needs.values()))[1]
    assert shared_filesystem_short(needs, {k: str(tmp_path) for k in needs}, free_of=lambda k: total - 1) != []


# 8 — a v1 site is named, not skipped in silence
def test_a_v1_site_is_named(tmp_path):
    from mmw.site import home as H
    from mmw.site.mset import open_set as _open
    H.init_site(tmp_path / "srv" / ".mmw" / "site", tmp_path / "srv")
    recents.remember(str(tmp_path / "srv"))
    with pytest.raises(LookupError, match="made before"):
        _open()


# 9 — hostnames are read once per root per set
def test_owner_reads_each_root_once(hosts, tmp_path, monkeypatch):  # noqa: F811
    _split(hosts, tmp_path)
    import mmw.site.mset as M
    calls = []
    real = M.hostnames
    monkeypatch.setattr(M, "hostnames", lambda home: calls.append(home) or real(home))
    m = open_set()
    for _ in range(5):
        m.owner(HOST_A)
        m.owner(HOST_B)
    assert len(calls) <= 2


# 12 — a repository's first file is counted as its own
def test_repo_event_comes_before_its_first_file(hosts, tmp_path):  # noqa: F811
    cfg, net, md = hosts
    events = []
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), place={"*": str(tmp_path / "r")},
                 say=quiet, progress=lambda e, **kw: events.append((e, kw.get("repo"), kw.get("name"))))
    for i, (e, repo, name) in enumerate(events):
        if e == "repo":
            assert events[i + 1][0] == "receive"                      # the repo is named first


# 16 — one root, however its path is spelled
def test_one_root_however_spelled(hosts, tmp_path):  # noqa: F811
    from mmw.tui.site.place import same_location
    assert same_location(str(tmp_path / "r1"), str(tmp_path / "x" / ".." / "r1"))
    assert not same_location(str(tmp_path / "r1"), str(tmp_path / "r2"))


# 22 — the ignored-runtime note survives whichever root is first
def test_runtime_note_is_the_sets(hosts, tmp_path):  # noqa: F811
    from test_site_runtime import NOTE, _with_runtime
    cfg, net, md = hosts
    b = _with_runtime(_bundle(cfg, net, md, tmp_path / "1", from_ref=None))
    r = apply_to_set(None, str(b), place={HOST_A: str(tmp_path / "a"), HOST_B: str(tmp_path / "b")}, say=quiet)
    assert r.actions.count(NOTE) == 1


# 1 — identity is written under each root's lock
def test_identity_written_under_the_lock(hosts, tmp_path):  # noqa: F811
    from mmw.site.lock import LockHeld, SiteLock
    *_, r1, r2, _r = _split(hosts, tmp_path)
    root = open_set().root_named("r2")
    with SiteLock(read_root(str(r2)).home):
        with pytest.raises(LockHeld):
            root.set_identity(members=["r1", "r2"])
