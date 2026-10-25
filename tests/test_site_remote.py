"""The remote side of an ssh root: verbs answered in JSON lines; an apply
checks, then waits for commit or abort."""
import io
from pathlib import Path
import json

from mmw.site import remote
from mmw.site.apply import apply_to_set
from mmw.site.db import SiteDB
from mmw.site.home import read_root

from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture
from test_site_apply import _bundle
from test_site_set_apply import hosts  # noqa: F401

quiet = lambda *a: None  # noqa: E731


def _call(argv, stdin=b""):
    out = io.BytesIO()
    code = remote.serve_root_command(argv, io.BytesIO(stdin), out)
    return code, out.getvalue()


def _lines(raw):
    return [json.loads(x) for x in raw.decode().splitlines() if x.strip()]


def _root(hosts, tmp_path):  # noqa: F811
    cfg, net, md = hosts
    r1 = tmp_path / "r1"
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), place={"*": str(r1)}, say=quiet)
    return cfg, net, md, r1


def test_info_round_trip(hosts, tmp_path):  # noqa: F811
    cfg, net, md, r1 = _root(hosts, tmp_path)
    code, out = _call(["info", str(r1)])
    (info,) = _lines(out)
    assert code == 0 and info["name"] == "r1" and info["network"] == net.name and info["protocol"] == 1
    assert set(info["hostnames"]) == {"example.com", "example.org"} and info["free"] > 0


def test_unknown_root_is_an_error_line(tmp_path):
    code, out = _call(["info", str(tmp_path)])
    (err,) = _lines(out)
    assert code == 1 and err["event"] == "error" and "no mirror" in err["message"]


def _apply_stream(bundle, control: bytes | None):
    """The bundle's members, framed as the local side sends them."""
    from mmw.site.apply import members_of
    buf = io.BytesIO()
    for m in members_of(str(bundle)):
        remote.write_member(buf, m.name, m.kind, m.fh, m.size, m.linkname)
    remote.write_end(buf)
    if control is not None:
        buf.write(control)
    return buf.getvalue()


def test_apply_two_phase_commit(hosts, tmp_path):  # noqa: F811
    from test_bundle_mmw import _change_b
    from test_snapshot_repo_versions import _change_a
    cfg, net, md, r1 = _root(hosts, tmp_path)
    _change_a(md)
    _change_b(md)
    b = _bundle(cfg, net, md, tmp_path / "2")
    code, out = _call(["apply", str(r1)], _apply_stream(b, b"commit\n"))
    events = _lines(out)
    assert code == 0, events
    kinds = [e["event"] for e in events]
    assert kinds.index("checked") < kinds.index("result")
    assert len(events[-1]["result"]["updated"]) == 2


def test_apply_abort_changes_nothing(hosts, tmp_path):  # noqa: F811
    from test_site_apply import _tree
    from test_snapshot_repo_versions import _change_a
    cfg, net, md, r1 = _root(hosts, tmp_path)
    _change_a(md)
    before = _tree(r1)
    code, out = _call(["apply", str(r1)], _apply_stream(_bundle(cfg, net, md, tmp_path / "2"), b"abort\n"))
    assert code == 0 and "result" not in [e["event"] for e in _lines(out)]
    assert _tree(r1) == before


def test_remote_dying_mid_stream_switches_nothing(hosts, tmp_path):  # noqa: F811
    from test_site_apply import _tree
    from test_snapshot_repo_versions import _change_a
    cfg, net, md, r1 = _root(hosts, tmp_path)
    _change_a(md)
    before = _tree(r1)
    raw = _apply_stream(_bundle(cfg, net, md, tmp_path / "2"), None)
    code, out = _call(["apply", str(r1)], raw[: len(raw) // 2])            # the connection drops
    assert code != 0 and _lines(out)[-1]["event"] == "error"
    assert _tree(r1) == before
    with SiteDB(read_root(str(r1)).home) as db:
        assert all(s.serving.number == 1 for s in db.repo_states())


def test_status_and_serve_verbs(hosts, tmp_path):  # noqa: F811
    cfg, net, md, r1 = _root(hosts, tmp_path)
    code, out = _call(["status", str(r1)])
    (rows,) = _lines(out)
    assert code == 0 and sorted(r["label"] for r in rows) == ["a", "b"]
    key = rows[0]["repo"]
    code, out = _call(["hold", str(r1), key])
    assert code == 0 and _lines(out)[0]["label"] == rows[0]["label"]


def test_hidden_cli_verb(hosts, tmp_path):  # noqa: F811
    import subprocess
    import sys
    cfg, net, md, r1 = _root(hosts, tmp_path)
    r = subprocess.run([str(Path(sys.executable).with_name("mmw")), "site", "_root", "info", str(r1)], capture_output=True)
    assert r.returncode == 0 and json.loads(r.stdout)["name"] == "r1"
    assert b"_root" not in subprocess.run([str(Path(sys.executable).with_name("mmw")), "site", "--help"], capture_output=True).stdout
