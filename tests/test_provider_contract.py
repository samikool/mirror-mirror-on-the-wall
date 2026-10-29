"""Every provider implements the whole contract, and core reaches provider
behaviour only through the registry."""
import pytest

from mmw.core import config, discover, probers
from mmw.providers import all_providers, get_provider
from mmw.providers.base import BaseProvider, Dimension


def test_registry_order_and_lookup():
    assert [p.name for p in all_providers()] == ["apt", "apt-flat", "apt-local", "vscode", "pypi"]
    assert get_provider("apt").name == "apt"
    with pytest.raises(ValueError, match="Unknown provider"):
        get_provider("nope")


@pytest.mark.parametrize("provider", all_providers(), ids=lambda p: p.name)
def test_every_provider_declares_its_metadata(provider: BaseProvider):
    assert provider.label and provider.description
    assert provider.dimensions and all(isinstance(d, Dimension) for d in provider.dimensions)
    # by-hash is apt's policy (its Release advertises it); VS Code has none.
    assert provider.by_hash is (provider.name in ("apt", "apt-flat"))


def test_apt_dimensions():
    apt = get_provider("apt")
    assert [d.name for d in apt.dimensions] == ["suites", "components", "arches"]
    assert [d.unit_key for d in apt.dimensions] == ["suite", "component", "arch"]
    assert apt.dimension("arches").absent_matches is True
    assert apt.dimension("suites").absent_matches is False
    assert apt.dimension("platforms") is None
    assert [d.name for d in get_provider("apt-flat").dimensions] == ["arches"]


def test_expand_and_covers_go_through_the_provider():
    src = config.SourceConfig(url="https://archive.ubuntu.com/ubuntu", suites=["noble"],
                              components=["main"], arches=["amd64", "arm64"])
    apt = get_provider("apt")
    units = apt.expand(src)
    assert [u.name for u in units] == [m.name for m in src.to_mirrors()]
    assert all(apt.covers(src, u) for u in units)


def test_only_probe_capable_providers_are_probers(monkeypatch):
    sentinel = discover.DetectResult(provider="apt")
    monkeypatch.setattr(discover, "detect_repository", lambda url: sentinel)
    assert len(probers.PROBERS) == 1
    assert probers.PROBERS[0]("https://x.example/deb") is sentinel
    assert not hasattr(get_provider("apt-flat"), "probe")


def test_core_resolution_dispatches_to_the_provider(monkeypatch):
    from mmw.core import resolve
    from mmw.core.resolve import ResolvedSource
    from mmw.providers import apt as apt_mod

    calls = []

    def fake(self, source, *, progress=None, context=None):
        calls.append(source.url)
        return ResolvedSource(source, [], ["note"], [])

    monkeypatch.setattr(apt_mod.AptProvider, "resolve", fake)
    src = config.SourceConfig(url="https://archive.ubuntu.com/ubuntu")
    units, notes, errors = resolve.resolve_sources([src])
    assert calls == ["https://archive.ubuntu.com/ubuntu"] and notes == ["note"]


def test_needs_resolving_asks_each_provider():
    from mmw.core import resolve
    flat = config.SourceConfig(url="https://x.example/repo/arm64", provider="apt-flat")
    open_apt = config.SourceConfig(url="https://archive.ubuntu.com/ubuntu")
    assert resolve.needs_resolving([flat]) is False
    assert resolve.needs_resolving([open_apt]) is True


def test_apt_resolution_no_longer_lives_in_core():
    from mmw.core import resolve
    assert not hasattr(resolve, "_resolve_source_legacy")


def test_verify_has_no_apt_code_left_in_core():
    from mmw.core import verify
    assert not hasattr(verify, "_verify_apt")
    assert not hasattr(verify, "_verify_apt_flat")
    assert not hasattr(verify, "_parse_apt_sha256")


def test_verify_reports_an_unknown_provider(tmp_path):
    from mmw.core.pool import Pool
    from mmw.core.verify import verify_mirror
    unit = config.MirrorConfig.model_construct(name="x", provider="nope", source_url="",
                                               source="", label="", dims={}, options={})
    with Pool(tmp_path, read_only=True) as pool:
        result = verify_mirror(unit, tmp_path, pool)
    assert result.error == "unknown provider 'nope'"


def test_apt_extra_hosts_follow_dist_upgrade():
    apt = get_provider("apt")
    on = config.NetworkConfig(name="lab", dist_upgrade=True)
    off = config.NetworkConfig(name="lab")
    assert apt.extra_hosts(on, []) == ["changelogs.ubuntu.com"]
    assert apt.extra_hosts(off, []) == []
    assert get_provider("apt-flat").extra_hosts(on, []) == []


def test_core_publish_has_no_apt_checks():
    import inspect
    from mmw.core import certs, publisher
    from mmw.core.publisher import links
    assert '("apt", "apt-flat")' not in inspect.getsource(links.publish_group)
    assert "dist_upgrade" not in inspect.getsource(publisher.walk_network)
    assert "dist_upgrade" not in inspect.getsource(certs.collect_hostnames)


def test_a_pre_seam_snapshot_regenerates_the_same_nginx():
    import json
    from mmw.core.publisher.nginx import recorded_nginx_files
    unit = config.MirrorConfig(name="u", provider="apt", source_url="https://archive.ubuntu.com/ubuntu",
                               suite="noble", component="main", arch="amd64")
    old = json.dumps({"network": {"name": "lab", "nginx": True, "dist_upgrade": True},
                      "mirrors": [{"name": "u", "provider": "apt",
                                   "source_url": "https://archive.ubuntu.com/ubuntu",
                                   "source": "", "label": "", "arch": "amd64",
                                   "suite": "noble", "component": "main"}],
                      "upstream_hosts": []})
    new = json.dumps({"network": {"name": "lab", "nginx": True, "dist_upgrade": True},
                      "mirrors": [unit.model_dump(mode="json")],
                      "upstream_hosts": [], "extra_hosts": ["changelogs.ubuntu.com"]})
    a = recorded_nginx_files(old, "/srv/lab")["nginx/mirror.conf"]
    b = recorded_nginx_files(new, "/srv/lab")["nginx/mirror.conf"]
    assert a == b and b"server_name changelogs.ubuntu.com;" in a


def test_destination_index_reading_lives_in_the_providers(tmp_path):
    import inspect
    from mmw.core.publisher import stats
    assert "provider ==" not in inspect.getsource(stats)
    unit = config.MirrorConfig(name="u", provider="apt", source_url="https://x/u",
                               suite="noble", component="main", arch="amd64")
    index = tmp_path / "dists" / "noble" / "main" / "binary-amd64" / "Packages"
    index.parent.mkdir(parents=True)
    index.write_text("Package: hello\nFilename: pool/h/hello.deb\nSHA256: abc\nSize: 3\n\n")
    apt = get_provider("apt")
    assert apt.published_index(tmp_path, unit) == index
    assert [p.checksum for p in apt.published_packages(index)] == ["abc"]
    flat = config.MirrorConfig(name="f", provider="apt-flat", source_url="https://x/f")
    assert get_provider("apt-flat").published_index(tmp_path, flat) is None


def test_resolve_sources_hands_every_provider_the_context(monkeypatch, tmp_path):
    from mmw.core import resolve
    from mmw.providers.base import ResolveContext
    seen = []
    for p in all_providers():
        # On the class: an instance attribute would outlive monkeypatch's undo.
        monkeypatch.setattr(type(p), "resolve", lambda self, src, *, progress=None, context=None:
                            seen.append((self.name, context)) or resolve.ResolvedSource(src, []))
    ctx = ResolveContext(mirror_dir=tmp_path, max_connections=3)
    resolve.resolve_sources([config.VscodeSource(provider="vscode")], context=ctx)
    assert seen == [("vscode", ctx)]


def test_resolve_context_from_settings(tmp_path):
    from mmw.providers.base import ResolveContext
    s = config.Settings(mirror_dir=tmp_path, max_connections=7)
    assert ResolveContext.from_settings(s) == ResolveContext(mirror_dir=tmp_path, max_connections=7)
