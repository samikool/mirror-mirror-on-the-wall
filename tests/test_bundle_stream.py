"""`mmw bundle --out -`: the bundle as a tar on stdout, so an ssh pipe
writes it straight onto a drive with no disk spent on the mirror host."""
import io
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from mmw.core import config
from mmw.core.pool import Pool
from mmw.core.publisher.bundle import bundle_network

from bundle_golden import describe
from test_bundle import seeded  # noqa: F401 — the shared pool fixture


def _net(tmp_path):
    return config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out", certs=False)


def test_stream_bundle_matches_file_bundle(seeded, tmp_path):  # noqa: F811
    mirror_dir, cfg = seeded
    net = _net(tmp_path)
    buf = io.BytesIO()
    r = bundle_network(net, cfg, mirror_dir, out_path=Path("-"), stdout=buf, from_ref=None)
    streamed = tmp_path / "streamed.tar"
    streamed.write_bytes(buf.getvalue())
    filed = bundle_network(net, cfg, mirror_dir, out_path=tmp_path / "f", from_ref=None, to_ref=r.created)
    assert describe(streamed) == describe(filed.out_path)
    assert not list(tmp_path.glob("*.manifest.tmp")) and not Path("-").exists()
    with Pool(mirror_dir) as p:
        log = p.bundles(net.name)
    assert (log[-1].kind, log[-1].out_path) == ("stream", "-")
    import hashlib
    assert r.stream_sha256 == hashlib.sha256(buf.getvalue()).hexdigest()


def test_stream_broken_pipe_leaves_no_ledger(seeded, tmp_path):  # noqa: F811
    mirror_dir, cfg = seeded

    class Broken(io.RawIOBase):
        def writable(self):
            return True

        def write(self, b):
            raise BrokenPipeError

    with pytest.raises(BrokenPipeError):
        bundle_network(_net(tmp_path), cfg, mirror_dir, out_path=Path("-"), stdout=Broken(), from_ref=None)
    with Pool(mirror_dir) as p:
        assert p.bundles("lab") == []


def _saved(seeded, tmp_path):  # noqa: F811
    mirror_dir, cfg = seeded
    cfg = cfg.model_copy(update={"networks": [_net(tmp_path)]})
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    config.save(cfg, cfg_dir)
    return cfg_dir


def test_cli_stream_keeps_stdout_pure(seeded, tmp_path):  # noqa: F811
    cfg_dir = _saved(seeded, tmp_path)
    out = subprocess.run([sys.executable, "-c", "from mmw.cli import app; app()", "--config", str(cfg_dir),
                          "bundle", "-n", "lab", "--out", "-", "--allow-incomplete"], capture_output=True)
    assert out.returncode == 0, out.stderr.decode()
    assert tarfile.open(fileobj=io.BytesIO(out.stdout)).getnames()[0] == ".mmw/manifest.jsonl"
    assert b"sha256 " in out.stderr


def test_cli_stream_broken_pipe_one_line(seeded, tmp_path):  # noqa: F811
    cfg_dir = _saved(seeded, tmp_path)
    cmd = (f"{sys.executable} -c 'from mmw.cli import app; app()' --config {cfg_dir} "
           "bundle -n lab --out - --allow-incomplete | head -c 1000 > /dev/null")
    out = subprocess.run(["bash", "-o", "pipefail", "-c", cmd], capture_output=True)
    err = out.stderr.decode()
    assert out.returncode != 0
    assert "nothing recorded" in err and "Traceback" not in err, err
    with Pool(seeded[0]) as p:
        assert p.bundles("lab") == []


def test_cli_stream_needs_a_network(seeded, tmp_path):  # noqa: F811
    cfg_dir = _saved(seeded, tmp_path)
    out = subprocess.run([sys.executable, "-c", "from mmw.cli import app; app()", "--config", str(cfg_dir),
                          "bundle", "--out", "-"], capture_output=True)
    assert out.returncode == 1 and b"-n" in out.stderr and out.stdout == b""


def test_stream_preflight_messages_go_to_stderr(seeded, tmp_path, monkeypatch, capsys):  # noqa: F811
    from mmw.commands import bundle as cmd
    from mmw.core import db_warm
    from mmw.core.publisher.results import BundleResult
    cfg_dir = _saved(seeded, tmp_path)
    monkeypatch.setattr(db_warm, "warm_for_job", lambda mirror_dir, say=None, **kw: say and say("warming the db"))
    monkeypatch.setattr(cmd, "bundle_network", lambda *a, **kw: BundleResult(network_name="lab", created="s"))
    assert cmd.run(cfg_dir, network="lab", out=Path("-"), allow_incomplete=True) == 0
    out = capsys.readouterr()
    assert "warming" not in out.out and "warming" in out.err
