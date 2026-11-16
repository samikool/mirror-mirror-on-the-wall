"""Writing a far side's export: a format-3 tar, manifest first, each file once
by checksum; the ledger only once it's whole."""
import json
import tarfile
import threading

import pytest

from mmw.core.publisher.manifest import blob_member
from mmw.site.db import SiteDB
from mmw.site.export import export_bundle
from mmw.site.home import read_root
from mmw.site.mset import open_set

from test_dry_run_totals import two_mirrors  # noqa: F401
from test_site_set_apply import hosts  # noqa: F401
from test_site_ssh_set import _split, fake_ssh  # noqa: F401


@pytest.fixture(autouse=True)
def _xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture
def one_root(two_mirrors, tmp_path):  # noqa: F811
    from mmw.site.apply import apply_to_set
    from test_bundle_mmw import _mmw
    from test_site_apply import _bundle
    cfg, net, md = _mmw(two_mirrors)
    root = tmp_path / "r1"
    apply_to_set(None, str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None)), place={"*": str(root)},
                 say=lambda *a: None)
    return root


def _members(path):
    with tarfile.open(path) as tar:
        names = tar.getnames()
        manifest = tar.extractfile(".mmw/manifest.jsonl").read().decode().splitlines()
    return names, json.loads(manifest[0]), [json.loads(x) for x in manifest[1:]]


def test_export_writes_a_format3_tar(one_root, tmp_path):
    out = tmp_path / "elm.tar"
    r = export_bundle(open_set([str(one_root)]), "elm", str(out))
    names, header, rows = _members(out)
    assert names[0] == ".mmw/manifest.jsonl"
    assert header["format"] == 3 and header["network"] == "oak" and header["file_count"] == len(names) - 1
    assert set(header["points"]) == {r for r in {row.get("r") for row in rows} if r}
    assert len(names) - 1 == len(set(names)) - 1 == len({row["c"] for row in rows})        # each file once
    assert all(n == ".mmw/manifest.jsonl" or n == blob_member(n.rsplit("/", 1)[-1]) for n in names)
    assert r.points == 2 and r.new_target and not (tmp_path / "elm.tar.tmp").exists()
    with SiteDB(read_root(str(one_root)).home) as db:
        assert db.export_targets() == ["elm"]


def test_an_aborted_export_records_nothing(one_root, tmp_path):
    out = tmp_path / "elm.tar"
    stop = threading.Event()

    def progress(event, **data):
        if event == "file":
            stop.set()
    with pytest.raises(ValueError, match="stopped"):
        export_bundle(open_set([str(one_root)]), "elm", str(out), progress=progress, stop_event=stop)
    assert not out.exists() and not (tmp_path / "elm.tar.tmp").exists()
    with SiteDB(read_root(str(one_root)).home) as db:
        assert db.export_targets() == []


def test_nothing_new_writes_nothing(one_root, tmp_path):
    export_bundle(open_set([str(one_root)]), "elm", str(tmp_path / "1.tar"))
    with pytest.raises(ValueError, match="nothing new for elm"):
        export_bundle(open_set([str(one_root)]), "elm", str(tmp_path / "2.tar"))
    assert not (tmp_path / "2.tar").exists()
    r = export_bundle(open_set([str(one_root)]), "elm", str(tmp_path / "3.tar"), mode="all")
    assert r.points == 2 and not r.new_target


def test_dry_run_says_size_and_writes_nothing(one_root, tmp_path):
    r = export_bundle(open_set([str(one_root)]), "elm", str(tmp_path / "x.tar"), dry_run=True)
    assert r.points == 2 and r.bytes > 0 and r.files > 0
    assert not (tmp_path / "x.tar").exists()
    with SiteDB(read_root(str(one_root)).home) as db:
        assert db.export_targets() == []


def test_export_spans_roots_including_ssh(hosts, tmp_path):  # noqa: F811
    cfg, net, md, r1, r2 = _split(hosts, tmp_path)
    out = tmp_path / "elm.tar"
    r = export_bundle(open_set(), "elm", str(out))
    _names, header, _rows = _members(out)
    assert {m["label"] for m in header["points"].values()} == {"a", "b"} and r.points == 2
    assert "farhost" in (tmp_path / "ssh.log").read_text()
    with SiteDB(read_root(str(r2)).home) as db:                  # the ssh root's own ledger
        assert db.export_targets() == ["elm"]


def test_the_cli_plans_an_export_once(one_root, tmp_path, monkeypatch):
    """The summary and the tar come from one plan: never planned twice."""
    from typer.testing import CliRunner

    from mmw.cli import app
    from mmw.site import export
    calls = []
    real = export.plan_root_export
    monkeypatch.setattr(export, "plan_root_export", lambda *a, **k: calls.append(1) or real(*a, **k))
    r = CliRunner().invoke(app, ["site", "bundle", "--to", "elm", "--out", str(tmp_path / "1.tar"),
                                 "--root", str(one_root)])
    assert r.exit_code == 0, r.output
    assert len(calls) == 1 and "point(s)" in r.output and (tmp_path / "1.tar").exists()


def test_a_root_that_cant_record_leaves_no_root_recording_it(hosts, tmp_path, monkeypatch):  # noqa: F811
    """An ssh drop while the record is written: no root keeps the record, the
    tar goes, and trying again sends the same."""
    from mmw.site.mset import LocalRoot
    from mmw.site.sshroot import SshRoot
    cfg, net, md, r1, r2 = _split(hosts, tmp_path)
    out = tmp_path / "elm.tar"
    originals = {kind: kind.record_export for kind in (LocalRoot, SshRoot)}
    calls = []                                                    # whichever records second fails

    def flaky(real):
        def record(self, *a, **k):
            calls.append(self.info.name)
            if len(calls) == 2:
                raise OSError("ssh: connection dropped")
            return real(self, *a, **k)
        return record
    monkeypatch.setattr(LocalRoot, "record_export", flaky(LocalRoot.record_export))
    monkeypatch.setattr(SshRoot, "record_export", flaky(SshRoot.record_export))
    with pytest.raises(ValueError, match="couldn't record it .*removed; try again"):
        export_bundle(open_set(), "elm", str(out))
    assert not out.exists()
    for root in (r1, r2):
        with SiteDB(read_root(str(root)).home) as db:
            assert db.exported("elm", next(iter(db.repo_states())).repo) == {}
    for kind, real in originals.items():
        monkeypatch.setattr(kind, "record_export", real)
    assert export_bundle(open_set(), "elm", str(out)).points == 2
