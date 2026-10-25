"""An ssh root in the mirror set, end to end through the fake ssh (the
"remote" runs here, so its folder can be checked directly)."""
import os
from pathlib import Path

import pytest

from mmw.site import ops, recents
from mmw.site.apply import apply_to_set
from mmw.site.db import SiteDB
from mmw.site.home import read_root
from mmw.site.mset import open_set

from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture
from test_site_apply import _bundle
from test_site_set_apply import HOST_A, HOST_B, hosts  # noqa: F401

FAKE = Path(__file__).parent / "fake_ssh"
quiet = lambda *a: None  # noqa: E731


@pytest.fixture(autouse=True)
def fake_ssh(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", f"{FAKE}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_LOG", str(tmp_path / "ssh.log"))
    monkeypatch.delenv("FAKE_SSH_MODE", raising=False)


def _split(hosts, tmp_path):  # noqa: F811
    cfg, net, md = hosts
    r1, r2 = tmp_path / "r1", tmp_path / "r2"
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)),
                 place={HOST_A: str(r1), HOST_B: f"farhost:{r2}"}, say=quiet)
    return cfg, net, md, r1, r2


def _labels(root):
    with SiteDB(read_root(str(root)).home) as db:
        return sorted(s.label for s in db.repo_states() if s.serving)


def test_split_across_local_and_ssh_root(hosts, tmp_path):  # noqa: F811
    cfg, net, md, r1, r2 = _split(hosts, tmp_path)
    assert (_labels(r1), _labels(r2)) == (["a"], ["b"])
    assert f"farhost:{r2}" in recents.known()
    m = open_set()
    assert sorted(r.info.name for r in m.roots) == ["r1", "r2"]
    assert sorted((root, st.label) for root, st in ops.set_status(m)) == [("r1", "a"), ("r2", "b")]
    assert "farhost" in (tmp_path / "ssh.log").read_text()          # it really went over ssh


def test_status_and_serve_through_ssh(hosts, tmp_path):  # noqa: F811
    from test_bundle_mmw import _change_b
    cfg, net, md, r1, r2 = _split(hosts, tmp_path)
    _change_b(md)
    apply_to_set(open_set(), str(_bundle(cfg, net, md, tmp_path / "2")), say=quiet)
    root, key = ops.on_repo(open_set(), "b")
    assert root.info.location == f"farhost:{r2}"
    root.serve(key, 1)
    with SiteDB(read_root(str(r2)).home) as db:
        st = db.repo_state(key)
        assert st.serving.number == 1 and st.held


def test_unreachable_ssh_root_is_missing(hosts, tmp_path, monkeypatch):  # noqa: F811
    cfg, net, md, r1, r2 = _split(hosts, tmp_path)
    monkeypatch.setenv("FAKE_SSH_MODE", "down")
    m = open_set()
    assert m.missing == ["r2"] and "can't reach farhost" in m.missing_reason["r2"]


def test_move_between_local_and_ssh_root(hosts, tmp_path):  # noqa: F811
    cfg, net, md, r1, r2 = _split(hosts, tmp_path)
    ops.move_repo(open_set(), "b", "r1")
    assert _labels(r1) == ["a", "b"]
    assert sorted((root, st.label) for root, st in ops.set_status(open_set())) == [("r1", "a"), ("r1", "b")]
    assert ops.tidy_set(open_set()) == ["b"]


def test_certs_over_ssh_never_move_a_key(hosts, tmp_path):  # noqa: F811
    from mmw.site import certs as C
    cfg, net, md, r1, r2 = _split(hosts, tmp_path)
    crt, key = C.make_ca(tmp_path / "ca", "Lab CA", None)
    C.load_ca(crt, key, None)
    log = tmp_path / "ssh.log"
    log.write_bytes(b"")
    signed = C.sign_set(open_set(), password=None)
    assert sorted((x.root, x.action) for x in signed) == [("r1", "signed"), ("r2", "signed")]
    assert (r2 / ".mmw" / "tls" / "mirror.crt").exists() and (r2 / ".mmw" / "tls" / "mirror.key").exists()
    crossed = log.read_bytes()
    assert b"CERTIFICATE REQUEST" in crossed and b"BEGIN CERTIFICATE" in crossed
    assert b"PRIVATE KEY" not in crossed                       # neither the CA's nor the root's
