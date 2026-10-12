"""NetworkSelect stores one list per provider dimension; TOML keeps flat keys."""
import pytest
from pydantic import ValidationError

from mmw.core import config
from mmw.core.config import MirrorConfig, NetworkSelect

APT = "https://archive.ubuntu.com/ubuntu"
FLAT = "https://developer.download.nvidia.com/hpc-sdk/ubuntu"


def _apt(suite, comp, arch):
    return MirrorConfig(name=f"u-{suite}-{comp}-{arch}", provider="apt", source=APT,
                        source_url=APT, suite=suite, component=comp, arch=arch)


def test_flat_keys_fold_into_dims():
    sel = NetworkSelect(source=APT, suites=["noble"], arches=["arm64"])
    assert sel.dims == {"suites": ["noble"], "arches": ["arm64"]}
    assert sel.suites == ["noble"] and sel.components == [] and sel.arches == ["arm64"]


def test_matches_like_before():
    sel = NetworkSelect(source=APT, suites=["noble"], arches=["arm64"])
    assert sel.matches(_apt("noble", "main", "arm64"))
    assert not sel.matches(_apt("jammy", "main", "arm64"))
    assert not sel.matches(_apt("noble", "main", "amd64"))
    no_suite = MirrorConfig(name="u", provider="apt", source=APT, source_url=APT, arch="arm64")
    assert not sel.matches(no_suite)          # suites: a unit without one is filtered out


def test_flat_unit_with_unknown_arch_passes_an_arch_filter():
    flat = MirrorConfig(name="f", provider="apt-flat", source=FLAT, source_url=FLAT, arch="")
    assert NetworkSelect(source=FLAT, arches=["arm64"]).matches(flat)


def test_unknown_select_key_is_a_load_error():
    with pytest.raises(ValidationError, match="suite"):
        NetworkSelect(source=APT, suite=["noble"])


def test_select_key_must_belong_to_the_sources_provider(tmp_path):
    with pytest.raises(ValidationError, match="components"):
        config.Config(
            settings=config.Settings(mirror_dir=tmp_path / "m"),
            sources=[config.SourceConfig(url=FLAT, provider="apt-flat")],
            networks=[config.NetworkConfig(
                name="lab", select=[NetworkSelect(source=FLAT, components=["main"])])],
        )


def test_save_writes_flat_keys_in_dimension_order(tmp_path):
    cfg = config.Config(
        settings=config.Settings(mirror_dir=tmp_path / "m"),
        sources=[config.SourceConfig(url=APT)],
        networks=[config.NetworkConfig(name="lab", select=[
            NetworkSelect(source=APT, arches=["arm64"], suites=["noble"])])],
    )
    config.save(cfg, tmp_path)
    text = (tmp_path / "mmw.toml").read_text()
    assert text.index('suites = [') < text.index('arches = [')
    assert "dims" not in text
    assert config.load(tmp_path).networks[0].select[0].dims == {
        "suites": ["noble"], "arches": ["arm64"]}


def test_an_empty_key_from_another_provider_still_loads(tmp_path):
    """`suites = []` on a flat source meant nothing before the seam and loaded;
    an empty list filters nothing, so it must keep loading."""
    (tmp_path / "mmw.toml").write_text(
        f'[settings]\nmirror_dir = "{tmp_path / "m"}"\n\n'
        f'[[sources]]\nurl = "{FLAT}"\nprovider = "apt-flat"\n\n'
        f'[[networks]]\nname = "lab"\n\n'
        f'[[networks.select]]\nsource = "{FLAT}"\nsuites = []\n')
    assert config.load(tmp_path).networks[0].select[0].source == FLAT
