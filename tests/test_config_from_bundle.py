"""Recover only configuration that the kept manifest actually recorded."""
import json
from pathlib import Path

import pytest

from mmw.core import config, snapshots
from mmw.core.pool import Pool
from mmw.core.publisher.manifest import ManifestHeader, write_manifest

from test_dry_run_totals import two_mirrors  # noqa: F401


def recorded_settings(tmp_path, monkeypatch):
    sources = [
        config.SourceConfig(url="https://deb.example/debian", name="debian", suites=["bookworm"],
                            components=["main"], arches=["amd64"], include_sources=True),
        config.VscodeSource(provider="vscode", kinds=["extensions"], platforms=["linux-x64"],
                            extensions={"prerelease": True}),
        config.PypiSource(provider="pypi", requirements=["six==1.17.0"], pythons=["3.12"],
                          platforms=["linux-glibc"], arches=["amd64"]),
        config.LocalAptSource(provider="apt-local", url="https://tools.lab/apt", folder="debs",
                              key="tools", structured=True),
    ]
    net = config.NetworkConfig(name="lab", history=True)
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "pool"), sources=sources,
                        networks=[net])
    # Exercise the real settings builder without fetching any provider's payload.
    import mmw.core.publisher as publisher
    monkeypatch.setattr(publisher, "walk_network", lambda *a, **kw: cfg.mirrors)
    with Pool(cfg.settings.mirror_dir) as pool:
        _, settings, _ = snapshots.walk_tree(net, cfg, cfg.settings.mirror_dir, pool)
    return cfg, json.loads(settings)


def test_recorded_settings_pin_what_non_apt_sources_lose(tmp_path, monkeypatch):
    cfg, settings = recorded_settings(tmp_path, monkeypatch)
    assert settings["network"] == cfg.networks[0].model_dump(mode="json")
    by_provider = {name: [u for u in settings["mirrors"] if u["provider"] == name]
                   for name in ("apt", "vscode", "pypi", "apt-local")}
    assert all(by_provider.values())
    apt = by_provider["apt"][0]
    assert (apt["source"], apt["suite"], apt["component"], apt["arch"], apt["include_sources"]) == (
        cfg.sources[0].identity, "bookworm", "main", "amd64", True)
    for name in ("vscode", "pypi", "apt-local"):
        for unit in by_provider[name]:
            assert "dims" not in unit and "options" not in unit
    assert "kind" not in by_provider["vscode"][0]
    assert "requirements" not in by_provider["pypi"][0]
    assert "folder" not in by_provider["apt-local"][0]
    assert "key" not in by_provider["apt-local"][0]


def manifest(path, settings, *, stamp="20261101T120000Z", fmt=3):
    write_manifest(path, ManifestHeader(settings["network"]["name"], stamp, None, True,
                                       format=fmt, settings=json.dumps(settings)), [])
    return path


def restore(paths, tmp_path):
    from mmw.core.config_from_bundle import config_from_bundles
    text, notes = config_from_bundles(paths, tmp_path / "restored-pool")
    return config.Config.model_validate(config.tomllib.loads(text)), text, notes


def test_missing_non_apt_fields_become_notes(tmp_path, monkeypatch):
    _, settings = recorded_settings(tmp_path, monkeypatch)
    back, _, notes = restore([manifest(tmp_path / "lab.manifest.jsonl", settings)], tmp_path)
    assert [s.provider for s in back.sources] == ["apt"]
    for name in ("vscode", "pypi", "apt-local"):
        assert any(name in note and "not recorded" in note for note in notes)
    assert any("global" in note for note in notes)
    assert any("networks" in note for note in notes)


@pytest.mark.parametrize("fmt", [2, 3])
def test_apt_settings_round_trip_the_same_units_and_network_fields(tmp_path, fmt):
    source = config.SourceConfig(url="https://deb.example/debian/", name="our-debian", label="debian",
                                 suites=["bookworm", "trixie"], components=["main"],
                                 suite_overrides=[config.SuiteOverride(suite="trixie", components=["main", "contrib"])],
                                 arches=["amd64", "arm64"], include_sources=True,
                                 gpg_key="public armour", gpg_key_name="debian", dist_upgrade=True,
                                 gpg_key_url="https://deb.example/keys/archive.gpg",
                                 extra_files=["https://deb.example/meta-release"])
    net = config.NetworkConfig(name="lab", root=tmp_path / "www", bundle_dir=tmp_path / "bundles",
                               history=True,
                               runtime_arches=["aarch64"], certs=False, retention="forever",
                               select=[config.NetworkSelect(source=source.identity, arches=["arm64"])])
    settings = {"network": net.model_dump(mode="json"), "mirrors": [u.legacy_dict() for u in source.to_mirrors()]}
    # Headers from before 1.5.7 carry the network's retired retention: read, then dropped.
    settings["network"].update(keep_points="all", keep_suites={"bookworm": 5})
    if fmt == 2:  # legacy headers stored the selections with flat dimension keys
        settings["network"]["select"] = [{"source": source.identity, "arches": ["arm64"]}]
        settings["network"]["far_side"] = "mmw"
    back, _, _ = restore([manifest(tmp_path / "lab.manifest.jsonl", settings, fmt=fmt)], tmp_path)
    assert back.sources[0].to_mirrors() == source.to_mirrors()
    assert back.networks == [net]


def test_real_bundle_round_trip(two_mirrors, tmp_path):  # noqa: F811
    from mmw.core.publisher.bundle import bundle_network
    from mmw.core.resolve import network_mirrors
    cfg, net, md = two_mirrors
    bundle = bundle_network(net, cfg, md, out_path=tmp_path / "kept")
    back, _, _ = restore([bundle.out_path], tmp_path)
    assert network_mirrors(back.networks[0], back, back.settings.mirror_dir) == network_mirrors(net, cfg, md)
    assert back.networks == [net]


def test_newest_bundle_per_network_wins_and_other_networks_accumulate(tmp_path):
    def settings(name, url, history):
        src = config.SourceConfig(url=url, provider="apt-flat")
        net = config.NetworkConfig(name=name, history=history)
        return {"network": net.model_dump(mode="json"), "mirrors": [u.legacy_dict() for u in src.to_mirrors()]}
    old = manifest(tmp_path / "old.manifest.jsonl", settings("lab", "https://old.example/apt", False))
    new = manifest(tmp_path / "new.manifest.jsonl", settings("lab", "https://new.example/apt", True), stamp="20261102T120000Z")
    other = manifest(tmp_path / "other.manifest.jsonl", settings("prod", "https://prod.example/apt", False))
    back, _, _ = restore([new, other, old], tmp_path)
    assert {n.name: n.history for n in back.networks} == {"lab": True, "prod": False}
    assert {s.url for s in back.sources} == {"https://new.example/apt", "https://prod.example/apt"}


@pytest.mark.parametrize("settings", [None, {}, {"network": {"name": "other"}, "mirrors": []}])
def test_missing_or_inconsistent_settings_are_refused(tmp_path, settings):
    from mmw.core.config_from_bundle import config_from_bundles
    p = tmp_path / "bad.manifest.jsonl"
    write_manifest(p, ManifestHeader("lab", "20261101T120000Z", None, True, format=2,
                                     settings=json.dumps(settings) if settings is not None else None), [])
    with pytest.raises(ValueError, match="settings|network"):
        config_from_bundles([p], Path("mirror"))



def test_invalid_recorded_selection_has_a_readable_error(tmp_path):
    settings = {"network": {"name": "lab", "select": ["not a selection"]}, "mirrors": []}
    with pytest.raises(ValueError, match="settings"):
        restore([manifest(tmp_path / "lab.manifest.jsonl", settings)], tmp_path)
