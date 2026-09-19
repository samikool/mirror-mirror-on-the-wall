from pathlib import Path

import pytest

from mmw.core import config


def _sample_config() -> config.Config:
    return config.Config(
        settings=config.Settings(mirror_dir=Path("/data/repos"), max_connections=7),
        sources=[
            config.SourceConfig(
                name="debian",
                label="debian",
                provider="apt",
                url="https://deb.debian.org/debian",
                arches=["amd64"],
                suites=["bookworm", "bookworm-updates"],
                components=["main", "contrib"],
                suite_overrides=[
                    config.SuiteOverride(suite="bookworm-updates", components=["main"]),
                ],
                include_sources=True,
            ),
            config.SourceConfig(
                name="microsoft-packages",
                label="microsoft",
                provider="apt",
                url="https://packages.microsoft.com/repos/code",
                arches=["amd64", "arm64"],
                suites=["stable"],
                components=["main"],
                gpg_key_url="https://example.com/key",
            ),
        ],
        networks=[
            config.NetworkConfig(
                name="lab", root=Path("/srv/lab"),
                select=[config.NetworkSelect(
                    source="https://deb.debian.org/debian",
                    suites=["bookworm"], arches=["amd64"],
                )],
                prune=False,
                dist_upgrade=True, certs=True,
            ),
        ],
    )


def test_save_load_round_trip(tmp_path):
    cfg = _sample_config()
    config.save(cfg, tmp_path)
    loaded = config.load(tmp_path)
    assert loaded == cfg


def test_load_missing_raises(tmp_path):
    import pytest
    with pytest.raises(FileNotFoundError):
        config.load(tmp_path)


def test_delete_is_idempotent(tmp_path):
    config.delete(tmp_path)  # nothing there — must not raise
    config.save(_sample_config(), tmp_path)
    config.delete(tmp_path)
    assert not config.config_path(tmp_path).exists()


def test_mirror_base():
    m = config.MirrorConfig(
        name="x", provider="apt", label="microsoft",
        source_url="https://packages.microsoft.com/repos/code/",
    )
    assert m.mirror_base == "packages.microsoft.com/repos/code"


def test_mirror_base_no_path():
    m = config.MirrorConfig(name="x", provider="apt", label="custom", source_url="https://host.example/")
    assert m.mirror_base == "host.example"


def test_apt_source_expansion_with_overrides():
    cfg = _sample_config()
    apt_mirrors = cfg.sources[0].to_mirrors()
    names = {m.name for m in apt_mirrors}
    # bookworm gets both components, bookworm-updates only main (override)
    # every apt mirror name ends in its arch — two arches of one suite/component
    # must never share a refs mirror_name
    assert names == {
        "debian-bookworm-main-amd64",
        "debian-bookworm-contrib-amd64",
        "debian-bookworm-updates-main-amd64",
    }
    assert all(m.include_sources for m in apt_mirrors)


def test_config_mirrors_property():
    cfg = _sample_config()
    assert len(cfg.mirrors) == 5  # 3 debian + 2 microsoft (one per arch)


def test_network_without_root_is_valid():
    """A network is a downselect of the mirror; destinations belong to verbs."""
    net = config.NetworkConfig(name="dgx-site")
    assert net.root is None
    assert net.bundle_dir is None
    assert net.nginx is True


def test_network_with_only_bundle_dir(tmp_path):
    net = config.NetworkConfig(name="dgx-site", bundle_dir=tmp_path / "out")
    assert net.root is None
    assert net.bundle_dir == tmp_path / "out"


def test_publish_base_rejects_rootless_network():
    from mmw.core.publisher import publish_base
    net = config.NetworkConfig(name="dgx-site")
    m = config.MirrorConfig(source_url="https://archive.ubuntu.com/ubuntu/",
                            name="m", provider="apt", label="ubuntu")
    with pytest.raises(ValueError, match="no publish root"):
        publish_base(net, m)


def test_save_load_round_trip_bundle_only_network(tmp_path):
    """A network with no publish root but a bundle_dir must round-trip as such
    — not as the literal string "None" written where a path belongs, and not
    silently dropping bundle_dir since it never had a serializer."""
    cfg = _sample_config()
    cfg.networks.append(
        config.NetworkConfig(name="dgx-site", bundle_dir=tmp_path / "out")
    )
    config.save(cfg, tmp_path)
    loaded = config.load(tmp_path)
    assert loaded == cfg
    bundle_only = [n for n in loaded.networks if n.name == "dgx-site"][0]
    assert bundle_only.root is None
    assert bundle_only.bundle_dir == tmp_path / "out"


def test_full_network_config_round_trip(tmp_path):
    """M1/M2 (whole-branch review) — every `NetworkConfig` field, set to a
    non-default value, must survive save()/load() with its actual value, not
    just its key. This is the exact class of bug that hid `root = "None"`
    and a never-serialized `bundle_dir` for ten implementation tasks: a
    round-trip test that never *sets* a field can't catch a regression in
    serializing it. `_sample_config`'s "lab" network above never set
    `bundle_dir` and never set `nginx=False`, so neither was ever exercised
    by `test_save_load_round_trip`."""
    net = config.NetworkConfig(
        name="dgx-site",
        root=tmp_path / "srv" / "dgx",
        bundle_dir=tmp_path / "out" / "dgx",
        select=[config.NetworkSelect(
            source="https://deb.debian.org/debian",
            suites=["bookworm"], components=["main"], arches=["amd64"],
        )],
        prune=False,
        dist_upgrade=True,
        certs=True,
        nginx=False,
        bundle_compress=False,
    )
    cfg = config.Config(
        settings=config.Settings(mirror_dir=tmp_path / "mirror", max_connections=3),
        networks=[net],
    )
    config.save(cfg, tmp_path)
    loaded = config.load(tmp_path)

    assert loaded == cfg
    got = loaded.networks[0]
    # Not just `==` on the whole object — pin every field's actual value so a
    # future field addition that forgets to set a non-default in this test
    # (and so trivially "round-trips" its own default) still gets caught by
    # someone reading this test, not just by object equality.
    assert got.name == "dgx-site"
    assert got.root == tmp_path / "srv" / "dgx"
    assert got.bundle_dir == tmp_path / "out" / "dgx"
    assert got.select == net.select
    assert got.prune is False
    assert got.dist_upgrade is True
    assert got.certs is True
    assert got.nginx is False
    assert got.bundle_compress is False
