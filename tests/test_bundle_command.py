"""`mmw bundle` — headless CLI door onto `bundle_network`. No network, no pool,
no tarballs: `bundle_network` itself is monkeypatched out so this only tests
the command's own config-loading, network-selection and reporting logic."""
from pathlib import Path

import pytest

from mmw.commands import bundle as bundle_cmd
from mmw.core import config
from mmw.core.publisher.results import BundleResult


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


def test_unknown_network_returns_1(cfg_dir, monkeypatch):
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(config.NetworkConfig(name="lab")))
    code = bundle_cmd.run(cfg_dir, network="nope")
    assert code == 1


def test_network_with_no_bundle_dir_is_skipped_when_iterating_all(cfg_dir, monkeypatch, capsys):
    """Iterating every network and meeting one with no bundle destination is
    not a failure — a publish-only network in a `mmw bundle` run is a perfectly
    normal config, and failing on it would make a nightly bundle exit 1 forever
    while doing exactly the right thing. `mmw publish` applies the identical
    rule to a network with no `root`; the two used to disagree, each claiming
    parity with the other."""
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(config.NetworkConfig(name="lab")))
    called = []
    monkeypatch.setattr(bundle_cmd, "bundle_network", lambda *a, **k: called.append(1))

    code = bundle_cmd.run(cfg_dir)

    assert code == 0
    assert not called
    out = capsys.readouterr().out
    assert "lab" in out
    assert "bundle_dir" in out or "--out" in out


def test_naming_a_network_with_no_bundle_dir_is_an_error(cfg_dir, monkeypatch, capsys):
    """Asking for one network by name and getting nothing done IS a failure:
    you asked for something this network cannot do. The bare-iteration case
    above is the one that must stay quiet."""
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(config.NetworkConfig(name="lab")))
    called = []
    monkeypatch.setattr(bundle_cmd, "bundle_network", lambda *a, **k: called.append(1))

    code = bundle_cmd.run(cfg_dir, network="lab")

    assert code == 1
    assert not called
    assert "lab" in capsys.readouterr().err


def test_second_network_still_bundles_after_first_is_skipped(cfg_dir, monkeypatch, capsys):
    """A multi-network cron run must not abort over one network with no
    destination — the whole point of processing all networks in one pass."""
    tmp = Path("/tmp/out")
    net_no_dest = config.NetworkConfig(name="lab-a")
    net_ok = config.NetworkConfig(name="lab-b", bundle_dir=tmp)
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(net_no_dest, net_ok))

    result = BundleResult(network_name="lab-b", out_path=tmp / "lab-b-20260919T000000Z.tar.zst",
                           sent=3, sent_bytes=100, deleted=1)
    calls = []

    def fake_bundle_network(net, cfg, mirror_dir, out_path=None, full=False, compress=True,
                            from_ref=None, to_ref="now"):
        calls.append((net.name, full, compress))
        return result

    monkeypatch.setattr(bundle_cmd, "bundle_network", fake_bundle_network)

    code = bundle_cmd.run(cfg_dir)

    assert code == 0  # lab-a's skip is not a failure; lab-b still bundled
    assert calls == [("lab-b", False, True)]
    out = capsys.readouterr().out
    assert "lab-a" in out
    assert str(result.out_path) in out


def test_successful_bundle_prints_path_and_forwards_full(cfg_dir, monkeypatch, capsys):
    net = config.NetworkConfig(name="lab", bundle_dir=Path("/tmp/out"))
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(net))

    result = BundleResult(network_name="lab", out_path=Path("/tmp/out/lab-20260919T000000Z.tar.zst"),
                           sent=42, sent_bytes=12345, deleted=2)
    calls = []

    def fake_bundle_network(net, cfg, mirror_dir, out_path=None, full=False, compress=True,
                            from_ref=None, to_ref="now"):
        # --full reaches the builder as "start from nothing".
        calls.append({"network": net.name, "out_path": out_path,
                      "full": full or from_ref is None, "compress": compress})
        return result

    monkeypatch.setattr(bundle_cmd, "bundle_network", fake_bundle_network)

    code = bundle_cmd.run(cfg_dir, full=True)

    assert code == 0
    assert calls == [{"network": "lab", "out_path": None, "full": True, "compress": True}]
    out = capsys.readouterr().out
    assert str(result.out_path) in out
    assert "sent=42" in out
    assert "deleted=2" in out


def test_exception_from_one_network_does_not_abort_the_run(cfg_dir, monkeypatch, capsys):
    net_a = config.NetworkConfig(name="lab-a", bundle_dir=Path("/tmp/out"))
    net_b = config.NetworkConfig(name="lab-b", bundle_dir=Path("/tmp/out"))
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(net_a, net_b))

    result_b = BundleResult(network_name="lab-b", out_path=Path("/tmp/out/lab-b.tar.zst"),
                             sent=1, sent_bytes=1, deleted=0)
    calls = []

    def fake_bundle_network(net, cfg, mirror_dir, out_path=None, full=False, compress=True,
                            from_ref=None, to_ref="now"):
        calls.append(net.name)
        if net.name == "lab-a":
            raise RuntimeError("pool is locked")
        return result_b

    monkeypatch.setattr(bundle_cmd, "bundle_network", fake_bundle_network)

    code = bundle_cmd.run(cfg_dir)

    assert code == 1
    assert calls == ["lab-a", "lab-b"]  # lab-b still ran after lab-a's exception
    captured = capsys.readouterr()
    # The failure goes to stderr and the successful network's line to stdout,
    # so a cron job redirecting only stderr to mail reports the one that broke
    # without also mailing the ones that worked.
    assert "pool is locked" in captured.err
    assert "lab-b" in captured.out


def test_out_option_overrides_missing_bundle_dir(cfg_dir, monkeypatch):
    """--out supplies a destination even when the network has none configured —
    the skip is about having *some* destination, not specifically bundle_dir."""
    net = config.NetworkConfig(name="lab")
    monkeypatch.setattr(config, "load", lambda cfg_dir: _cfg(net))

    result = BundleResult(network_name="lab", out_path=Path("/tmp/explicit/lab.tar.zst"))
    calls = []

    def fake_bundle_network(net, cfg, mirror_dir, out_path=None, full=False, compress=True,
                            from_ref=None, to_ref="now"):
        calls.append(out_path)
        return result

    monkeypatch.setattr(bundle_cmd, "bundle_network", fake_bundle_network)

    code = bundle_cmd.run(cfg_dir, out=Path("/tmp/explicit"))

    assert code == 0
    assert calls == [Path("/tmp/explicit")]
