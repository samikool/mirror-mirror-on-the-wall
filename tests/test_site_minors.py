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


# 14 — nginx_steps says start or recreate itself
def test_nginx_steps_carry_their_verb(tmp_path):
    from mmw.site.apply import ApplyResult, SetApplyResult, nginx_steps
    r = SetApplyResult(per_root={"oak": ApplyResult(network_changed=True), "new": ApplyResult()},
                       paths={"oak": "/srv/oak", "new": "/srv/new"}, made=["new"])
    got = {name: verb for name, verb, _cmds in nginx_steps(r)}
    assert got == {"oak": "recreate", "new": "start"}


# 15, 18 — a bundle's sizes are read once per process, not per screen
def test_bundle_sizes_read_once(hosts, tmp_path, monkeypatch):  # noqa: F811
    import json
    import tarfile
    from mmw.site import apply as A
    cfg, net, md = hosts
    b = _bundle(cfg, net, md, tmp_path / "1", from_ref=None)
    calls = []
    real = A._scan_sizes
    monkeypatch.setattr(A, "_scan_sizes", lambda *a: calls.append(1) or real(*a))
    A._manifest_and_sizes(str(b), tmp_path / "m1.jsonl")
    A._manifest_and_sizes(str(b), tmp_path / "m2.jsonl")
    assert len(calls) == 1 and (tmp_path / "m2.jsonl").exists()


# 17 — a stored blob whose size is wrong is replaced, not trusted
def test_a_damaged_stored_blob_is_replaced(tmp_path):
    import hashlib
    import io
    from mmw.site.store import Store
    s = Store(tmp_path)
    data = b"x" * 1000
    c, _ = s.add_stream(io.BytesIO(data))
    s.path(c).chmod(0o644)
    s.path(c).write_bytes(b"x" * 10)                                 # bitrot: truncated
    assert s.add_stream(io.BytesIO(data), expect=c, size=1000) == (c, 1000)        # as an apply calls it
    assert hashlib.sha256(s.path(c).read_bytes()).hexdigest() == c


# 20 — a remote speaking another protocol is refused
def test_protocol_mismatch_is_refused(monkeypatch):
    from mmw.site import ssh, sshroot
    monkeypatch.setattr(ssh, "check_remote_mmw", lambda host: None)
    monkeypatch.setattr(sshroot, "_call", lambda *a, **k: {"protocol": 99, "name": "x"})
    with pytest.raises(ssh.RemoteMmw, match="protocol"):
        sshroot.remote_info("farhost:/x")


# 21 — shared content is spooled beside a root, not in /tmp
def test_spool_lives_beside_a_root(hosts, tmp_path, monkeypatch):  # noqa: F811
    from mmw.site import apply as A
    cfg, net, md = hosts
    seen = []
    real = A._Spool.__init__
    monkeypatch.setattr(A._Spool, "__init__", lambda self, path: seen.append(path) or real(self, path))
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)),
                 place={HOST_A: str(tmp_path / "a"), HOST_B: str(tmp_path / "b")}, say=quiet)
    assert seen and str(seen[0]).startswith(str(tmp_path))


# 10 — what the deleted v1 tests covered, covered again at set level
def test_tidy_keeps_files_still_served(hosts, tmp_path):  # noqa: F811
    from test_site_apply import _tree
    cfg, net, md, r1, r2, _ = _split(hosts, tmp_path)
    ops.move_repo(open_set(), "b", "r1")
    keys_before = {p for p in _tree(r2) if p.startswith("keys/")}
    ops.tidy_set(open_set())
    assert {p for p in _tree(r2) if p.startswith("keys/")} == keys_before


def test_stdin_bundle_to_a_set(hosts, tmp_path, monkeypatch):  # noqa: F811
    import io
    import sys
    cfg, net, md = hosts
    b = _bundle(cfg, net, md, tmp_path / "1", from_ref=None)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(b.read_bytes())))
    r = apply_to_set(None, "-", place={"*": str(tmp_path / "r")}, say=quiet)
    assert len(r.merged().updated) == 2


def test_shared_content_crosses_roots(hosts, tmp_path):  # noqa: F811
    """shared.deb is in both repositories, under two hostnames in two roots."""
    cfg, net, md, r1, r2, _ = _split(hosts, tmp_path)
    assert (r1 / "example.com" / "a" / "shared.deb").read_bytes() == (r2 / "example.org" / "b" / "shared.deb").read_bytes()


# 11 — a move says how much it copies before it starts
def test_move_size_before_moving(hosts, tmp_path):  # noqa: F811
    cfg, net, md, r1, r2, _ = _split(hosts, tmp_path)
    assert 0 < ops.move_size(open_set(), "b", "r1")


# 19 — a remote's own error reaches the user, never as "damaged"
def test_remote_export_error_is_said(hosts, tmp_path, monkeypatch):  # noqa: F811
    from test_site_ssh_set import FAKE
    monkeypatch.setenv("PATH", f"{FAKE}:{os.environ['PATH']}")
    cfg, net, md = hosts
    r2 = tmp_path / "r2"
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), place={"*": f"farhost:{r2}"},
                 say=quiet)
    root = open_set().roots[0]
    with pytest.raises(ValueError, match="no blob"):
        with root.export_blob("ab" * 32) as fh:
            fh.read()


# 23 — progress from a reader thread lands on the screen
def test_progress_from_another_thread(tmp_path):
    import asyncio

    from mmw.tui.site.apply_flow import ApplyChoice, ApplyRunPage
    from mmw.tui.ui.base import ThemedApp
    from waiting import until

    class Quiet(ApplyRunPage):
        def _run(self):
            pass

    async def go():
        app = ThemedApp()
        async with app.run_test(size=(120, 40)) as pilot:
            page = Quiet(ApplyChoice("b.tar", frozenset(), 1, (("ka", "a"),)))
            app.push_screen(page)
            await until(pilot, lambda: page.query("#run-repos"))
            t = threading.Thread(target=page._progress, args=("switch",), kwargs={"repo": "ka", "label": "a"})
            t.start()
            await until(pilot, lambda: "switching" in str(page.query_one("#run-repos").render()))
            t.join()
    asyncio.run(go())


# WSL: the Linux disk says ~1 TB free whatever the Windows drive has — Sam's
# second WSL fill-up. The smaller of the two is the truth.
def test_wsl_free_space_counts_the_windows_drive(monkeypatch, tmp_path):
    from collections import namedtuple

    from mmw.core import disk_io
    from mmw.site import space
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(space, "_wsl", lambda: True)
    monkeypatch.setattr(space, "_windows_drive_free", lambda: 50 * 10**9)
    monkeypatch.setattr(disk_io.ops, "disk_usage", lambda p: Usage(10**13, 0, 10**12))
    assert space.real_free(tmp_path) == 50 * 10**9                    # the Windows drive, not WSL's 1 TB
    monkeypatch.setattr(space, "_existing", lambda p: Path("/mnt/d/mirror"))
    monkeypatch.setattr(Path, "resolve", lambda self: self)
    assert space.real_free(Path("/mnt/d/mirror")) == 10**12           # a Windows drive itself: as reported
    monkeypatch.undo()
    monkeypatch.setattr(space, "_wsl", lambda: False)
    monkeypatch.setattr(disk_io.ops, "disk_usage", lambda p: Usage(10**13, 0, 10**12))
    monkeypatch.setattr(space, "_wsl", lambda: False)
    assert space.real_free(tmp_path) == 10**12


# An interrupted first apply, then the folder wiped (Sam's WSL retry).
def test_a_failed_first_apply_leaves_no_memory(hosts, tmp_path):  # noqa: F811
    cfg, net, md = hosts
    stop = threading.Event()
    with pytest.raises(ApplyError):
        apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), place={"*": str(tmp_path / "r")},
                     say=quiet, stop_event=stop, progress=lambda e, **kw: e == "receive" and stop.set())
    assert str(tmp_path / "r") not in recents.known()               # remembered only once it committed


def test_a_deleted_root_starts_fresh(hosts, tmp_path):  # noqa: F811
    import shutil
    cfg, net, md = hosts
    b = str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None))
    r = tmp_path / "r"
    apply_to_set(None, b, place={"*": str(r)}, say=quiet)
    shutil.rmtree(r)                                                  # gone: never a mount point
    result = apply_to_set(None, b, place={"*": str(r)}, say=quiet)
    assert any("was a root here once and is gone — starting a new one" in line for line in result.lines)
    assert read_root(str(r)) is not None


def test_an_emptied_root_asks_before_applying(hosts, tmp_path):  # noqa: F811
    import shutil

    from mmw.site.apply import MaybeUnmounted
    cfg, net, md = hosts
    b = str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None))
    r = tmp_path / "r"
    apply_to_set(None, b, place={"*": str(r)}, say=quiet)
    for child in r.iterdir():                                         # still there, empty: maybe not mounted
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    with pytest.raises(MaybeUnmounted) as e:
        apply_to_set(None, b, place={"*": str(r)}, say=quiet)
    assert e.value.locations == [str(r)] and not any(r.iterdir())    # nothing written
    apply_to_set(None, b, place={"*": str(r)}, say=quiet, apply_anyway={str(r)})
    assert read_root(str(r)) is not None


def test_cli_asks_or_takes_yes(hosts, tmp_path):  # noqa: F811
    import shutil

    from typer.testing import CliRunner

    from mmw import cli
    cfg, net, md = hosts
    b = str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None))
    r = tmp_path / "r"
    apply_to_set(None, b, place={"*": str(r)}, say=quiet)
    for child in r.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    out = CliRunner().invoke(cli.app, ["site", "apply", b, "--root", f"*={r}"])
    assert out.exit_code == 5 and "--yes" in out.output and "isn't mounted" in out.output
    out = CliRunner().invoke(cli.app, ["site", "apply", b, "--root", f"*={r}"], input="y\n",
                             env={"MMW_ASSUME_TTY": "1"})
    assert out.exit_code == 0, out.output
