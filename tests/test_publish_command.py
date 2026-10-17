"""`mmw publish --quiet`/`--dry-run` — headless CLI door onto `publish_network`/
`dry_run_network`. No network, no pool, no filesystem writes: both publisher
functions are monkeypatched out so this only tests the command's own
config-loading, network-selection and reporting logic."""
from pathlib import Path

import pytest

from mmw.commands import publish as publish_cmd
from mmw.core import config
from mmw.core.publisher.results import MirrorPublishResult, NetworkPublishResult
from test_bundle import seeded  # noqa: F401 — the shared seeded-pool fixture


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


def test_network_with_no_root_is_skipped_and_never_prints_none(cfg_dir, monkeypatch, capsys):
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(config.NetworkConfig(name="lab")))
    called = []
    monkeypatch.setattr(publish_cmd, "publish_network", lambda *a, **k: called.append(1))

    code = publish_cmd.run(cfg_dir)

    assert code == 0
    assert not called
    out = capsys.readouterr().out
    assert "lab" in out
    assert "None" not in out


def test_unknown_network_returns_1(cfg_dir, monkeypatch):
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(config.NetworkConfig(name="lab")))
    code = publish_cmd.run(cfg_dir, network="nope")
    assert code == 1


def test_dry_run_calls_dry_run_network_never_publish_network(cfg_dir, monkeypatch):
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"))
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(net))

    dry_calls = []
    publish_calls = []
    result = NetworkPublishResult(network_name="lab", dry_run=True)
    monkeypatch.setattr(publish_cmd, "dry_run_network",
                         lambda *a, **k: (dry_calls.append(1), result)[1])
    monkeypatch.setattr(publish_cmd, "publish_network",
                         lambda *a, **k: publish_calls.append(1))

    code = publish_cmd.run(cfg_dir, dry_run=True)

    assert code == 0
    assert dry_calls == [1]
    assert not publish_calls


def test_network_exception_goes_to_stderr_and_next_network_still_publishes(cfg_dir, monkeypatch, capsys):
    """A multi-network cron run must not abort over one network raising — the
    same guard `commands/bundle.py` already has, for the same reason: a cron
    job redirecting only stderr to mail should get the network that broke,
    not lose every network after it to an unhandled traceback."""
    net_a = config.NetworkConfig(name="lab-a", root=Path("/srv/a"))
    net_b = config.NetworkConfig(name="lab-b", root=Path("/srv/b"))
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(net_a, net_b))

    result_b = NetworkPublishResult(
        network_name="lab-b",
        mirrors=[MirrorPublishResult(mirror_name="lab-b-main", linked=5)],
    )
    calls = []

    def fake_publish_network(net, cfg, mirror_dir, **_kw):
        calls.append(net.name)
        if net.name == "lab-a":
            raise RuntimeError("pool is locked")
        return result_b

    monkeypatch.setattr(publish_cmd, "publish_network", fake_publish_network)

    code = publish_cmd.run(cfg_dir)

    assert code == 1
    assert calls == ["lab-a", "lab-b"]  # lab-b still ran after lab-a's exception
    captured = capsys.readouterr()
    assert "pool is locked" in captured.err
    assert "pool is locked" not in captured.out
    assert "lab-b" in captured.out


def test_per_mirror_errors_go_to_stderr_and_fail_the_run(cfg_dir, monkeypatch, capsys):
    """A publish that reported errors must exit non-zero.

    This is a deliberate change from the pre-move behaviour, not a
    preservation of it. The old inline body had no exit-code logic at all,
    so a publish printing "✗ boom: disk full" still returned 0 and looked
    like success to the cron job that ran it — the exact silent-failure mode
    `mmw sync --quiet` is documented against and `commands/bundle.py`
    already guards. Publish was the one verb that didn't.
    """
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"))
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(net))

    result = NetworkPublishResult(
        network_name="lab",
        mirrors=[MirrorPublishResult(mirror_name="lab-main", errors=["boom: disk full"])],
    )
    monkeypatch.setattr(publish_cmd, "publish_network", lambda *a, **k: result)

    code = publish_cmd.run(cfg_dir)

    assert code == 1
    captured = capsys.readouterr()
    assert "boom: disk full" in captured.err
    assert "boom: disk full" not in captured.out


def test_a_clean_publish_still_exits_zero(cfg_dir, monkeypatch):
    """The other half of the contract: tightening the error path must not
    make an ordinary successful publish start failing cron."""
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"))
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(net))

    result = NetworkPublishResult(
        network_name="lab",
        mirrors=[MirrorPublishResult(mirror_name="lab-main", linked=5)],
    )
    monkeypatch.setattr(publish_cmd, "publish_network", lambda *a, **k: result)

    assert publish_cmd.run(cfg_dir) == 0


def test_dist_upgrade_warnings_go_to_stderr_and_do_not_fail(cfg_dir, monkeypatch, capsys):
    net = config.NetworkConfig(name="lab", root=Path("/srv/lab"))
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(net))

    result = NetworkPublishResult(
        network_name="lab",
        dist_upgrade_warnings=["jammy present without jammy-updates"],
    )
    monkeypatch.setattr(publish_cmd, "publish_network", lambda *a, **k: result)

    code = publish_cmd.run(cfg_dir)

    assert code == 0
    captured = capsys.readouterr()
    assert "⚠" in captured.err
    assert "jammy present without jammy-updates" in captured.err
    assert "jammy present without jammy-updates" not in captured.out


def _seeded_cfg(seeded, tmp_path):  # noqa: F811
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", root=tmp_path / "pub", nginx=False)
    cfg = cfg.model_copy(update={"networks": [net]})
    config.save(cfg, tmp_path)
    return mirror_dir, net


def test_full_rechecks_through_publish_network(seeded, tmp_path, monkeypatch):  # noqa: F811
    _seeded_cfg(seeded, tmp_path)
    seen = []
    real = publish_cmd.publish_network
    monkeypatch.setattr(publish_cmd, "publish_network",
                        lambda *a, **k: (seen.append(k.get("full")), real(*a, **k))[1])
    assert publish_cmd.run(tmp_path, network="lab", full=True, allow_incomplete=True) == 0
    assert seen == [True]


def test_take_snapshot_snapshots_once_before_publishing(seeded, tmp_path):  # noqa: F811
    from mmw.core.pool import Pool
    mirror_dir, _net = _seeded_cfg(seeded, tmp_path)
    assert publish_cmd.run(tmp_path, network="lab", take_snapshot=True, allow_incomplete=True) == 0
    with Pool(mirror_dir, read_only=True) as pool:
        snaps = pool.snapshots("lab")
    assert len(snaps) == 1 and snaps[0].kind == "manual"
    assert publish_cmd.run(tmp_path, network="lab", allow_incomplete=True) == 0
    with Pool(mirror_dir, read_only=True) as pool:
        assert len(pool.snapshots("lab")) == 1, "a plain publish never snapshots"


def test_take_snapshot_with_a_snapshot_target_is_refused(seeded, tmp_path, capsys):  # noqa: F811
    _seeded_cfg(seeded, tmp_path)
    assert publish_cmd.run(tmp_path, network="lab", snapshot="x", take_snapshot=True) == 1
    assert "nothing to snapshot" in capsys.readouterr().err

