"""Per-network layers: custom debs added to an upstream mirror a network publishes."""
import pytest
from pydantic import ValidationError

from mmw.core import config
from mmw.core.layers import layer_dir
from mmw.core.pool import KIND_META, Pool

UBUNTU = "http://archive.ubuntu.com/ubuntu"


def test_layer_config_round_trips_and_refuses_bad_entries(tmp_path):
    layer = config.NetworkLayer(source=UBUNTU, signing="resign", key="tools", contents=True)
    net = config.NetworkConfig(name="lab", layers=[layer])
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path), networks=[net])
    config.save(cfg, tmp_path)
    text = (tmp_path / "mmw.toml").read_text()
    assert "[[networks.layers]]" in text and "drop_valid_until" not in text
    assert config.load(tmp_path).networks[0].layers == [layer]
    with pytest.raises(ValidationError, match="key"):
        config.NetworkLayer(source=UBUNTU, signing="resign")
    with pytest.raises(ValidationError, match="one layer"):
        config.NetworkConfig(name="lab", layers=[config.NetworkLayer(source=UBUNTU, signing="unsigned"),
                                                 config.NetworkLayer(source=UBUNTU + "/", signing="unsigned")])
    assert config.NetworkConfig(name="lab").layer_for(UBUNTU) is None
    assert net.layer_for(UBUNTU + "/").key == "tools"


def test_layer_dir_is_per_network_and_repository(tmp_path):
    assert layer_dir(tmp_path, "lab", "ubuntu-archive") == tmp_path / "layers/lab/ubuntu-archive"


def test_pool_layer_overlay_round_trip_rename_and_forget(tmp_path):
    with Pool(tmp_path) as pool:
        assert pool.layer_overlay("lab", "archive.ubuntu.com/ubuntu") == {}
        assert pool.layer_digest("lab", "archive.ubuntu.com/ubuntu") is None
        blob = pool.add(b"Packages body", "a" * 64, "sha256", "Packages", 13)
        pool.replace_layer("lab", "archive.ubuntu.com/ubuntu", [
            ("dists/jammy/main/binary-amd64/Packages", "a" * 64, 13, KIND_META, "Packages"),
            ("dists/jammy/InRelease", "", 0, "", ""),
        ], "digest-1")
        overlay = pool.layer_overlay("lab", "archive.ubuntu.com/ubuntu")
        assert overlay["dists/jammy/InRelease"] is None
        row = overlay["dists/jammy/main/binary-amd64/Packages"]
        assert row == ("dists/jammy/main/binary-amd64/Packages", str(blob), 13, KIND_META, "a" * 64)
        assert pool.layer_digest("lab", "archive.ubuntu.com/ubuntu") == "digest-1"
        assert pool.rename_network("lab", "lab2") is None
        assert pool.layer_overlay("lab2", "archive.ubuntu.com/ubuntu")
        assert pool.layer_overlay("lab", "archive.ubuntu.com/ubuntu") == {}
        pool.forget_network("lab2")
        assert pool.layer_overlay("lab2", "archive.ubuntu.com/ubuntu") == {}
        assert pool.layer_digest("lab2", "archive.ubuntu.com/ubuntu") is None


def test_renaming_a_network_moves_its_layer_folder(tmp_path):
    (layer_dir(tmp_path, "lab", "u") / "jammy/main").mkdir(parents=True)
    problems = config.rename_network_state(tmp_path, "lab", "lab2")
    assert problems == [] and (layer_dir(tmp_path, "lab2", "u") / "jammy/main").is_dir()
