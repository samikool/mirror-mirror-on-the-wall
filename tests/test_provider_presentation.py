"""Pure, Textual-free presentation members: what the TUI renders comes from
the provider. Values pin today's apt rendering exactly."""
from mmw.core import config
from mmw.providers import get_provider
from mmw.providers.base import SourceDescription

APT = "https://Archive.Ubuntu.com/ubuntu"
FLAT = "https://developer.download.nvidia.com/hpc-sdk/ubuntu/arm64"


def test_dimension_display_prefers_short():
    apt = get_provider("apt")
    assert [d.display for d in apt.dimensions] == ["suites", "comps", "arches"]


def test_group_is_the_lowercased_host():
    assert get_provider("apt").group(config.SourceConfig(url=APT)) == "archive.ubuntu.com"


def test_apt_describe():
    src = config.SourceConfig(url=APT, suites=["noble"], components=[], arches=["amd64"],
                              include_sources=True, dist_upgrade=True)
    d = get_provider("apt").describe(src)
    assert d == SourceDescription(
        fields=[("Provider", "apt"), ("Label", "ubuntu"), ("Arches", ["amd64"]),
                ("Suites", ["noble"]), ("Components", [])],
        flags=["deb-src", "dist-upgrade"],
        location_label="Repository", location=APT)


def test_flat_describe_has_no_suites_or_components():
    d = get_provider("apt-flat").describe(config.SourceConfig(url=FLAT, provider="apt-flat"))
    assert [label for label, _ in d.fields] == ["Provider", "Label", "Arches"]
    assert d.flags == []


def test_unit_label_and_describe():
    apt = get_provider("apt")
    m = config.MirrorConfig(name="u-noble-main-arm64", provider="apt", source_url=APT,
                            suite="noble", component="main", arch="arm64")
    assert apt.unit_label(m) == ("noble / main", "arm64")
    assert apt.unit_describe(m) == [("Arch", "arm64"), ("Suite", "noble"), ("Component", "main")]
    bare = config.MirrorConfig(name="u-arm64", provider="apt", source_url=APT, arch="arm64")
    assert apt.unit_label(bare) is None
    assert apt.unit_describe(bare) == [("Arch", "arm64")]


def test_expansion_known():
    apt, flat = get_provider("apt"), get_provider("apt-flat")
    assert apt.expansion_known(config.SourceConfig(url=APT, arches=["amd64"])) is True
    assert apt.expansion_known(config.SourceConfig(url=APT)) is False
    assert apt.expansion_known(
        config.SourceConfig(url=APT, suites=["noble"], arches=["amd64"])) is False
    assert flat.expansion_known(config.SourceConfig(url=FLAT, provider="apt-flat")) is False
    assert flat.expansion_known(
        config.SourceConfig(url=FLAT, provider="apt-flat", arches=["arm64"])) is True


def test_dry_run_previews_have_no_apt_checks_in_core_or_tui():
    import inspect
    from mmw.core.publisher import stats
    from mmw.tui import publish_flows
    assert "dist_upgrade_info" not in inspect.getsource(stats)
    assert "dist_upgrade_info" not in inspect.getsource(publish_flows)


def test_preview_all_runs_the_apt_preview(tmp_path, monkeypatch):
    from mmw.core.pool import Pool
    from mmw.core.publisher import dist_upgrade
    from mmw.core.publisher.results import NetworkPublishResult
    from mmw.providers import preview_all
    monkeypatch.setattr(dist_upgrade, "dist_upgrade_info",
                        lambda net, cfg, pool, md: ([("jammy", "noble")], ["w"]))
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path))
    result = NetworkPublishResult(network_name="lab", dry_run=True)
    with Pool(tmp_path, read_only=True) as pool:
        preview_all(config.NetworkConfig(name="lab", dist_upgrade=True), cfg, pool, tmp_path, result)
    assert result.dist_upgrade_paths == [("jammy", "noble")]
    assert result.dist_upgrade_warnings == ["w"]


def test_a_dry_run_opens_the_pool_once(tmp_path, monkeypatch):
    from mmw.core.pool import Pool
    from mmw.core.publisher import stats
    opened = []

    class Counting(Pool):
        def __init__(self, *a, **kw):
            opened.append(1)
            super().__init__(*a, **kw)

    monkeypatch.setattr(stats, "Pool", Counting)
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"))
    stats.dry_run_network(config.NetworkConfig(name="lab", root=tmp_path / "pub"),
                          cfg, tmp_path / "m")
    assert len(opened) == 1
