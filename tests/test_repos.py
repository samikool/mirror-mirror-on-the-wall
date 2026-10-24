"""Repositories are what end users revert, so every published base is
attributed to one: an apt source by its URL, all of VS Code as one, all of
PyPI as one."""
import json

from mmw.core import config, repos, snapshots
from mmw.core.snapshots import take_snapshot
from mmw.providers import get_provider

from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture
from test_pypi_publish import _cfg as _pypi_cfg, _src as _pypi_src, _sync as _pypi_sync


def test_repository_key_scopes():
    apt = config.SourceConfig(name="a", label="x", provider="apt-flat", url="https://example.com/a/")
    apt_unit = get_provider("apt-flat").expand(apt)[0]
    vs_unit = get_provider("vscode").expand(
        config.VscodeSource(provider="vscode", kinds=["server"], platforms=["linux-x64"]))[0]
    py_unit = get_provider("pypi").expand(_pypi_src())[0]
    assert repos.repository_key(apt_unit) == apt_unit.source
    assert repos.repository_key(vs_unit) == "vscode"
    assert repos.repository_key(py_unit) == "pypi"


def test_nested_and_shared_host_bases_attribute_by_longest_prefix():
    rb = {"nv-a": ["developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64"],
          "nv-b": ["developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/sbsa"],
          "outer": ["developer.download.nvidia.com/compute"]}
    assert repos.attribute("developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/sbsa/Packages",
                           rb) == "nv-b"
    assert repos.attribute("developer.download.nvidia.com/compute/other/x.deb", rb) == "outer"
    assert repos.attribute("keys/nvidia/a.gpg", rb) is None
    assert repos.attribute("developer.download.nvidia.com/computer/x", rb) is None   # prefix, not a path


def test_apt_snapshot_records_one_repo_per_source(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    snap = take_snapshot(net, cfg, mirror_dir)
    rr = snapshots.recorded_repos(snap.settings)
    assert set(rr) == {s.identity for s in cfg.sources}
    assert {b for bs in rr.values() for b in bs} <= set(json.loads(snap.settings)["bases"])


def test_pypi_snapshot_records_one_repo(tmp_path, up):
    cfg = _pypi_cfg(tmp_path, _pypi_src())
    _pypi_sync(cfg)
    snap = take_snapshot(cfg.networks[0], cfg, cfg.settings.mirror_dir)
    rr = snapshots.recorded_repos(snap.settings)
    assert list(rr) == ["pypi"]
    assert set(rr["pypi"]) == set(json.loads(snap.settings)["bases"])


def test_recorded_repos_derives_for_old_settings(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    snap = take_snapshot(net, cfg, mirror_dir)
    data = json.loads(snap.settings)
    recorded = snapshots.recorded_repos(snap.settings)
    del data["repos"]
    assert snapshots.recorded_repos(json.dumps(data)) == recorded


def test_repository_label():
    apt = config.SourceConfig(name="debian", label="x", provider="apt-flat", url="https://example.com/a/")
    cfg = config.Config(settings=config.Settings(mirror_dir="/m"), sources=[apt])
    assert repos.repository_label(apt.identity, cfg) == "debian"
    assert repos.repository_label("vscode", cfg) == "VS Code"
    assert repos.repository_label("pypi", cfg) == "PyPI"
    assert repos.repository_label("https://gone.example/", cfg) == "https://gone.example/"


def test_resolve_repo_refs():
    available = {"https://example.com/a": "a", "vscode": "VS Code"}
    assert repos.resolve_repo_refs(["vs code", "https://example.com/a"], available) == \
        ["vscode", "https://example.com/a"]
    import pytest
    with pytest.raises(LookupError, match="this network has: a, VS Code"):
        repos.resolve_repo_refs(["nope"], available)
    with pytest.raises(LookupError, match="ambiguous"):
        repos.resolve_repo_refs(["x"], {"k1": "X", "k2": "x"})
