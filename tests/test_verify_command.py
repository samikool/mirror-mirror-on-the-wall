"""`mmw verify` — headless door onto `core.verify.verify_mirror`. `_verify_all`
is the seam every test here stubs, so none of them ever construct a real
`Pool` (see `commands/verify.py` for why that split exists)."""
from pathlib import Path

import pytest

from mmw.commands import verify as verify_cmd
from mmw.core import config
from mmw.core.verify import VerifyResult


@pytest.fixture
def cfg_dir(tmp_path: Path) -> Path:
    (tmp_path / "mmw.toml").write_text(
        '[settings]\nmirror_dir = "/tmp/pool"\nmax_connections = 8\n'
    )
    return tmp_path


def _cfg(*networks: config.NetworkConfig) -> config.Config:
    return config.Config(
        settings=config.Settings(mirror_dir=Path("/tmp/pool")),
        sources=[],
        networks=list(networks),
    )


def test_verify_all_complete_exits_0(cfg_dir, monkeypatch, capsys):
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg())
    results = [
        VerifyResult("debian-bookworm-main-amd64", "apt", total=100, present=100),
        VerifyResult("ubuntu-noble-main-amd64", "apt", total=50, present=50),
    ]
    monkeypatch.setattr(verify_cmd, "_verify_all", lambda cfg, mirror_dir, network: results)

    code = verify_cmd.run(cfg_dir)

    assert code == 0
    out = capsys.readouterr().out
    assert "debian-bookworm-main-amd64" in out
    assert "complete" in out
    assert "2/2 mirror(s) complete" in out


def test_verify_missing_blobs_exits_1_and_reports_count(cfg_dir, monkeypatch, capsys):
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg())
    results = [
        VerifyResult("ubuntu-noble-main-amd64", "apt", total=100, present=97, missing_count=3),
    ]
    monkeypatch.setattr(verify_cmd, "_verify_all", lambda cfg, mirror_dir, network: results)

    code = verify_cmd.run(cfg_dir)

    assert code == 1
    err = capsys.readouterr().err
    assert "ubuntu-noble-main-amd64" in err
    assert "3" in err
    assert "100" in err


def test_verify_network_option_narrows_to_that_network(cfg_dir, monkeypatch):
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"))
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(net))
    seen = {}

    def fake_verify_all(cfg, mirror_dir, network):
        seen["network"] = network
        return [VerifyResult("ubuntu-noble-main-amd64", "apt", total=10, present=10)]
    monkeypatch.setattr(verify_cmd, "_verify_all", fake_verify_all)

    code = verify_cmd.run(cfg_dir, network="lab")

    assert code == 0
    assert seen["network"] == "lab"


def test_verify_unknown_network_returns_1_without_calling_verify_all(cfg_dir, monkeypatch, capsys):
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"))
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(net))
    called = []
    monkeypatch.setattr(verify_cmd, "_verify_all", lambda *a, **k: called.append(1))

    code = verify_cmd.run(cfg_dir, network="nope")

    assert code == 1
    assert not called
    assert "nope" in capsys.readouterr().err


def test_verify_no_mirrors_exits_0(cfg_dir, monkeypatch, capsys):
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg())
    monkeypatch.setattr(verify_cmd, "_verify_all", lambda cfg, mirror_dir, network: [])

    code = verify_cmd.run(cfg_dir)

    assert code == 0
    assert "No mirrors to verify" in capsys.readouterr().out


def test_verify_reports_mirror_with_error(cfg_dir, monkeypatch, capsys):
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg())
    results = [VerifyResult("broken-mirror", "apt", error="Packages index not in pool — has this mirror been synced?")]
    monkeypatch.setattr(verify_cmd, "_verify_all", lambda cfg, mirror_dir, network: results)

    code = verify_cmd.run(cfg_dir)

    assert code == 1
    err = capsys.readouterr().err
    assert "broken-mirror" in err
    assert "not in pool" in err
