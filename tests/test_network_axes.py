"""Network axes and select summaries follow the provider's dimensions."""
from mmw.core import config
from mmw.tui.network_screens import build_axes, select_dims_line

APT = "https://archive.ubuntu.com/ubuntu"
FLAT = "https://developer.download.nvidia.com/hpc-sdk/ubuntu/arm64"


def _apt(suite, comp, arch):
    return config.MirrorConfig(name=f"u-{suite}-{comp}-{arch}", provider="apt", source=APT,
                               source_url=APT, suite=suite, component=comp, arch=arch)


def test_axes_offer_each_provider_dimension():
    flat = config.MirrorConfig(name="f", provider="apt-flat", source=FLAT, source_url=FLAT,
                               arch="arm64")
    axes = {a.source: a for a in build_axes(
        [_apt("noble", "main", "amd64"), _apt("jammy", "main", "arm64"), flat], None)}
    apt = axes[APT]
    assert [d.name for d in apt.dimensions] == ["suites", "components", "arches"]
    assert apt.values == {"suites": ["jammy", "noble"], "components": ["main"],
                          "arches": ["amd64", "arm64"]}
    assert [d.name for d in axes[FLAT].dimensions] == ["arches"]
    assert axes[FLAT].values == {"arches": ["arm64"]}


def test_select_line_uses_provider_order_and_short_names():
    srcs = [config.SourceConfig(url=APT)]
    sel = config.NetworkSelect(source=APT, arches=["arm64"], suites=["noble"])
    assert select_dims_line(sel, srcs, short=True) == [
        ("suites", ["noble"]), ("comps", []), ("arches", ["arm64"])]
    assert select_dims_line(sel, srcs, short=False)[1] == ("components", [])


def test_select_line_for_an_unconfigured_source_uses_the_provider_its_keys_fit():
    """A select whose source is gone (hand-edited) still renders every axis,
    with short names — as it did before the seam."""
    sel = config.NetworkSelect(source="https://gone.example/repo", suites=["x"], arches=[])
    assert select_dims_line(sel, [], short=True) == [
        ("suites", ["x"]), ("comps", []), ("arches", [])]
    bare = config.NetworkSelect(source="https://gone.example/repo")
    assert select_dims_line(bare, [], short=True) == [
        ("suites", []), ("comps", []), ("arches", [])]
