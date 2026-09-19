"""`mmw certs` — headless door onto `core.certs.generate_certs`, and the hook
`commands/publish.py` calls after a clean `certs = true` publish. No network,
no pool, no real key generation: `generate_certs` is monkeypatched out in
every test here, so this only tests config-loading, network-selection and
reporting logic — RSA keygen is slow and this suite is meant to stay fast."""
from pathlib import Path

import pytest

from mmw.commands import certs as certs_cmd
from mmw.commands import publish as publish_cmd
from mmw.core import config
from mmw.core.publisher.results import NetworkPublishResult


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


def _fake_generate_certs(hostnames):
    def fn(net, cfg):
        out_dir = Path("/tmp/pool/certs") / net.name
        return (
            hostnames,
            out_dir / "mirror.key",
            out_dir / "mirror.cnf",
            out_dir / "mirror.csr",
            out_dir / "CERTS.md",
        )
    return fn


# --- mmw certs -------------------------------------------------------------

def test_certs_command_generates_and_reports(cfg_dir, monkeypatch, capsys):
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"), certs=True)
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(net))
    monkeypatch.setattr(certs_cmd, "generate_certs", _fake_generate_certs(["archive.ubuntu.com"]))

    code = certs_cmd.run(cfg_dir)

    assert code == 0
    out = capsys.readouterr().out
    assert "lab" in out
    assert "archive.ubuntu.com" in out
    assert "mirror.key" in out
    assert "mirror.csr" in out


def test_certs_command_skips_network_without_certs_flag(cfg_dir, monkeypatch, capsys):
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"), certs=False)
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(net))
    called = []
    monkeypatch.setattr(certs_cmd, "generate_certs", lambda *a: called.append(1))

    code = certs_cmd.run(cfg_dir)

    assert not called
    assert code == 0
    assert "No networks have certs" in capsys.readouterr().out


def test_certs_command_named_network_without_certs_flag_reports_and_fails(cfg_dir, monkeypatch, capsys):
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"), certs=False)
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(net))
    called = []
    monkeypatch.setattr(certs_cmd, "generate_certs", lambda *a: called.append(1))

    code = certs_cmd.run(cfg_dir, network="lab")

    assert not called
    assert code == 1
    err = capsys.readouterr().err
    assert "lab" in err
    assert "certs = true" in err


def test_certs_command_unknown_network_returns_1(cfg_dir, monkeypatch):
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(config.NetworkConfig(name="lab")))
    code = certs_cmd.run(cfg_dir, network="nope")
    assert code == 1


def test_certs_command_reports_exception_and_returns_1(cfg_dir, monkeypatch, capsys):
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"), certs=True)
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(net))

    def boom(net, cfg):
        raise ValueError("No hostnames found — network has no mirrors configured")
    monkeypatch.setattr(certs_cmd, "generate_certs", boom)

    code = certs_cmd.run(cfg_dir)

    assert code == 1
    assert "No hostnames found" in capsys.readouterr().err


# --- generate_for ------------------------------------------------------------

def test_generate_for_catches_exception_and_returns_empty_list(monkeypatch, capsys):
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"), certs=True)
    cfg = _cfg(net)

    def boom(net, cfg):
        raise RuntimeError("disk full")
    monkeypatch.setattr(certs_cmd, "generate_certs", boom)

    result = certs_cmd.generate_for(net, cfg)

    assert result == []
    assert "disk full" in capsys.readouterr().err


# --- headless publish hook ---------------------------------------------------

def test_publish_calls_generate_for_when_certs_true(cfg_dir, monkeypatch):
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"), certs=True)
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(net))
    monkeypatch.setattr(publish_cmd, "publish_network",
                         lambda *a, **k: NetworkPublishResult(network_name="lab"))
    calls = []
    monkeypatch.setattr(publish_cmd, "generate_for", lambda net, cfg: calls.append(net.name))

    code = publish_cmd.run(cfg_dir)

    assert code == 0
    assert calls == ["lab"]


def test_publish_does_not_call_generate_for_when_certs_false(cfg_dir, monkeypatch):
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"), certs=False)
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(net))
    monkeypatch.setattr(publish_cmd, "publish_network",
                         lambda *a, **k: NetworkPublishResult(network_name="lab"))
    calls = []
    monkeypatch.setattr(publish_cmd, "generate_for", lambda net, cfg: calls.append(net.name))

    publish_cmd.run(cfg_dir)

    assert not calls


def test_publish_dry_run_never_calls_generate_for(cfg_dir, monkeypatch):
    """The whole point of a dry run is that it writes nothing — a dry run
    must never generate real key material even for a certs=True network."""
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"), certs=True)
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(net))
    monkeypatch.setattr(publish_cmd, "dry_run_network",
                         lambda *a, **k: NetworkPublishResult(network_name="lab", dry_run=True))
    calls = []
    monkeypatch.setattr(publish_cmd, "generate_for", lambda net, cfg: calls.append(net.name))

    publish_cmd.run(cfg_dir, dry_run=True)

    assert not calls


def test_publish_survives_certs_failure_and_still_reports_publish_result(cfg_dir, monkeypatch, capsys):
    """A certs step that fails must not take down a publish that already
    succeeded — `generate_for` swallows its own exceptions and returns [],
    and the publish result is reported exactly as if certs had never run."""
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"), certs=True)
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(net))
    monkeypatch.setattr(publish_cmd, "publish_network",
                         lambda *a, **k: NetworkPublishResult(network_name="lab"))

    def failing_generate_for(net, cfg):
        print(f"Certs for network '{net.name}' failed: boom", file=__import__("sys").stderr)
        return []
    monkeypatch.setattr(publish_cmd, "generate_for", failing_generate_for)

    code = publish_cmd.run(cfg_dir)

    assert code == 0
    captured = capsys.readouterr()
    assert "boom" in captured.err
    assert "lab" in captured.out
