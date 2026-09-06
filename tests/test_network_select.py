"""Publish-side selection by dimension.

`NetworkConfig.mirrors` was a frozen list of mirror names. Once an architecture
appeared upstream, its new mirrors were simply not in that list, so the network
**silently stopped publishing them** and said nothing. Selecting by dimension
keeps "this network is amd64" true as the repository grows.
"""
import hashlib

from mmw.core import config, resolve
from mmw.core.pool import KIND_PKG, Pool

URL = "http://archive.example.com/ubuntu"
OTHER = "http://packages.example.com/repos/code"


def _seed(mirror_dir, source, suites, components, arches):
    """Pretend a sync happened, recording mirrors the way sync_engine does."""
    with Pool(mirror_dir) as pool:
        for suite in suites:
            for comp in components:
                for arch in arches:
                    name = f"{source.split('/')[2].split('.')[0]}-{suite}-{comp}-{arch}"
                    cs = hashlib.sha256(name.encode()).hexdigest()
                    pool.add(name.encode(), cs, "sha256", f"{name}.deb", len(name))
                    pool.record_mirror(config.MirrorConfig(
                        name=name, provider="apt", source_url=source, source=source,
                        arch=arch, suite=suite, component=comp, label="x",
                    ))
                    pool.reconcile_refs(name, [(cs, f"pool/{name}.deb", KIND_PKG)], source)


def _cfg(tmp_path, *urls, select=None):
    return config.Config(
        settings=config.Settings(mirror_dir=tmp_path),
        sources=[config.SourceConfig(url=u) for u in urls],
        networks=[config.NetworkConfig(
            name="lab", root=tmp_path / "pub", select=select or [],
        )],
    )


def test_no_select_publishes_everything_mirrored(tmp_path):
    _seed(tmp_path, URL, ["noble"], ["main"], ["amd64", "arm64"])
    cfg = _cfg(tmp_path, URL)
    names = resolve.network_mirror_names(cfg.networks[0], cfg, tmp_path)
    assert len(names) == 2


def test_arch_filter_narrows(tmp_path):
    _seed(tmp_path, URL, ["noble"], ["main"], ["amd64", "arm64", "i386"])
    cfg = _cfg(tmp_path, URL, select=[
        config.NetworkSelect(source=URL, arches=["amd64"]),
    ])
    mirrors = resolve.network_mirrors(cfg.networks[0], cfg, tmp_path)
    assert {m.arch for m in mirrors} == {"amd64"}


def test_a_new_arch_still_reaches_an_all_arches_network(tmp_path):
    """The bug this replaces: a frozen name list would not contain the new
    mirrors, and nothing would say so."""
    _seed(tmp_path, URL, ["noble"], ["main"], ["amd64"])
    cfg = _cfg(tmp_path, URL, select=[config.NetworkSelect(source=URL)])  # all
    assert len(resolve.network_mirrors(cfg.networks[0], cfg, tmp_path)) == 1

    _seed(tmp_path, URL, ["noble"], ["main"], ["arm64"])   # upstream grew
    assert len(resolve.network_mirrors(cfg.networks[0], cfg, tmp_path)) == 2


def test_a_pinned_arch_network_ignores_the_new_arch(tmp_path):
    """"This network is amd64" must stay true, not silently widen."""
    _seed(tmp_path, URL, ["noble"], ["main"], ["amd64"])
    cfg = _cfg(tmp_path, URL, select=[
        config.NetworkSelect(source=URL, arches=["amd64"]),
    ])
    _seed(tmp_path, URL, ["noble"], ["main"], ["arm64"])
    mirrors = resolve.network_mirrors(cfg.networks[0], cfg, tmp_path)
    assert {m.arch for m in mirrors} == {"amd64"}


def test_select_only_matches_its_own_repository(tmp_path):
    _seed(tmp_path, URL, ["noble"], ["main"], ["amd64"])
    _seed(tmp_path, OTHER, ["stable"], ["main"], ["amd64"])
    cfg = _cfg(tmp_path, URL, OTHER, select=[config.NetworkSelect(source=URL)])
    mirrors = resolve.network_mirrors(cfg.networks[0], cfg, tmp_path)
    assert {m.source for m in mirrors} == {URL}


def test_select_source_is_normalized(tmp_path):
    """A trailing slash or upper-case host must not silently match nothing."""
    _seed(tmp_path, URL, ["noble"], ["main"], ["amd64"])
    cfg = _cfg(tmp_path, URL, select=[
        config.NetworkSelect(source="HTTP://Archive.Example.com/ubuntu/"),
    ])
    assert len(resolve.network_mirrors(cfg.networks[0], cfg, tmp_path)) == 1


def test_suite_and_component_filters_compose(tmp_path):
    _seed(tmp_path, URL, ["noble", "jammy"], ["main", "universe"], ["amd64"])
    cfg = _cfg(tmp_path, URL, select=[
        config.NetworkSelect(source=URL, suites=["noble"], components=["main"]),
    ])
    mirrors = resolve.network_mirrors(cfg.networks[0], cfg, tmp_path)
    assert [(m.suite, m.component) for m in mirrors] == [("noble", "main")]


def test_select_round_trips_through_the_config_file(tmp_path):
    cfg = _cfg(tmp_path, URL, select=[
        config.NetworkSelect(source=URL, suites=["noble"], arches=["amd64"]),
    ])
    config.save(cfg, tmp_path)
    loaded = config.load(tmp_path)
    sel = loaded.networks[0].select[0]
    assert sel.source == URL
    assert sel.suites == ["noble"]
    assert sel.arches == ["amd64"]
    assert sel.components == []      # "all" is stored by omission
