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


# ── merge must never narrow: empty means all on every dimension ──────────────

def test_merge_never_narrows_open_suites_or_components():
    """`_union` where `_union_or_all` belonged, on two dimensions out of three.

    A repository mirroring *everything* (suites/components/arches all open)
    merged with an import naming `jammy`/`main` came back as
    `suites=['jammy'] components=['main']` — an import silently narrowed a
    mirror from the whole archive to one suite of one component. Arches were
    already right, which is what made it look fine.
    """
    existing = [config.SourceConfig(url="http://archive.ubuntu.com/ubuntu")]
    incoming = [config.SourceConfig(
        url="http://archive.ubuntu.com/ubuntu",
        suites=["jammy"], components=["main"], arches=["amd64"],
    )]
    merged, added, changed = config.merge_sources(existing, incoming)
    assert added == 0 and len(merged) == 1
    assert merged[0].suites == [], "an import must never narrow 'all suites'"
    assert merged[0].components == [], "nor 'all components'"
    assert merged[0].arches == []


def test_merge_the_other_way_round_is_also_open():
    """The open side may be the incoming one — a bulk import of a whole
    sources.list.d commonly leaves dimensions open."""
    existing = [config.SourceConfig(
        url="http://archive.ubuntu.com/ubuntu", suites=["jammy"], components=["main"])]
    incoming = [config.SourceConfig(url="http://archive.ubuntu.com/ubuntu")]
    merged, _, _ = config.merge_sources(existing, incoming)
    assert merged[0].suites == [] and merged[0].components == []
    assert merged[0].suite_overrides == []


def test_merge_with_open_suites_still_unions_stated_components():
    """"All suites" does not mean "all components" — only an empty component
    list does. Two explicit component lists still union."""
    existing = [config.SourceConfig(
        url="http://archive.ubuntu.com/ubuntu", components=["main"])]
    incoming = [config.SourceConfig(
        url="http://archive.ubuntu.com/ubuntu", components=["universe"])]
    merged, _, _ = config.merge_sources(existing, incoming)
    assert merged[0].suites == []
    assert merged[0].components == ["main", "universe"]


def test_merge_keeps_per_suite_merging_when_both_sides_state_suites():
    existing = [config.SourceConfig(
        url="http://archive.ubuntu.com/ubuntu",
        suites=["jammy"], components=["main"])]
    incoming = [config.SourceConfig(
        url="http://archive.ubuntu.com/ubuntu",
        suites=["noble"], components=["main", "universe"])]
    merged, _, _ = config.merge_sources(existing, incoming)
    assert merged[0].suites == ["jammy", "noble"]
    assert merged[0].components == ["main"]
    assert [(o.suite, o.components) for o in merged[0].suite_overrides] == [
        ("noble", ["main", "universe"])
    ]


def test_merge_carries_dist_upgrade_across():
    existing = [config.SourceConfig(url="http://archive.ubuntu.com/ubuntu")]
    incoming = [config.SourceConfig(
        url="http://archive.ubuntu.com/ubuntu", dist_upgrade=True)]
    merged, _, _ = config.merge_sources(existing, incoming)
    assert merged[0].dist_upgrade is True


def test_merge_refuses_a_provider_mismatch():
    """One URI is one repository — it cannot be both flat and dists/-shaped."""
    existing = [config.SourceConfig(url="http://x.example/repo", provider="apt")]
    incoming = [config.SourceConfig(url="http://x.example/repo", provider="apt-flat")]
    with pytest.raises(ValueError, match="apt-flat"):
        config.merge_sources(existing, incoming)


# ── save() must never write a file load() rejects ────────────────────────────

def test_save_refuses_to_write_a_config_that_cannot_be_loaded(tmp_path):
    """pydantic does not re-validate on assignment, so any in-place edit could
    produce a file that saves fine and then locks the user out of every
    command."""
    cfg = _sample_config()
    cfg.sources[1].name = cfg.sources[0].name      # duplicate source name
    with pytest.raises(ValueError, match="unloadable"):
        config.save(cfg, tmp_path)
    assert not config.config_path(tmp_path).exists(), "nothing may reach disk"


def test_save_refuses_duplicate_network_names(tmp_path):
    cfg = _sample_config()
    cfg.networks.append(cfg.networks[0].model_copy(deep=True))
    with pytest.raises(ValueError, match="unloadable"):
        config.save(cfg, tmp_path)


def test_save_still_writes_a_legitimate_config(tmp_path):
    cfg = _sample_config()
    config.save(cfg, tmp_path)
    assert config.load(tmp_path) == cfg


# ── uniqueness: networks and repository identities too ───────────────────────

def test_two_networks_of_one_name_are_rejected():
    """They share `certs/{name}/` and one bundle ledger, and `mmw publish -n`
    silently picks the first."""
    with pytest.raises(Exception, match="duplicate network name"):
        config.Config(
            settings=config.Settings(mirror_dir=Path("/data")),
            networks=[config.NetworkConfig(name="lab"), config.NetworkConfig(name="lab")],
        )


def test_two_sources_of_one_identity_are_rejected():
    with pytest.raises(Exception, match="duplicate source URI"):
        config.Config(
            settings=config.Settings(mirror_dir=Path("/data")),
            sources=[
                config.SourceConfig(name="a", url="http://archive.ubuntu.com/ubuntu"),
                config.SourceConfig(name="b", url="HTTP://Archive.Ubuntu.com/ubuntu/"),
            ],
        )


# ── normalize_url folds default ports ────────────────────────────────────────

def test_default_ports_are_not_an_identity():
    n = config.normalize_url
    assert n("http://host:80/debian") == n("http://host/debian")
    assert n("https://host:443/") == n("https://host")
    assert n("HTTP://Host:80/Debian/") == "http://host/Debian"


def test_a_non_default_port_is_still_an_identity():
    n = config.normalize_url
    assert n("http://host:8080/debian") != n("http://host/debian")
    assert n("http://host:443/debian") != n("https://host:443/debian")


# ── provider is a closed vocabulary ──────────────────────────────────────────

def test_a_misspelled_provider_is_a_load_error(tmp_path):
    """`apt_flat` loaded clean, expanded to zero mirrors, and `mmw sync
    --quiet` exited 0 having mirrored nothing."""
    (tmp_path / "mmw.toml").write_text(
        '[settings]\nmirror_dir = "/data"\n\n'
        '[[sources]]\nurl = "http://x.example/repo"\nprovider = "apt_flat"\n'
    )
    with pytest.raises(Exception, match="provider"):
        config.load(tmp_path)


# ── a network name is a directory name ───────────────────────────────────────

def test_a_network_name_cannot_escape_its_directory():
    with pytest.raises(Exception, match="invalid network name"):
        config.NetworkConfig(name="../../../tmp/pwn")


def test_a_network_name_cannot_be_dot_dot():
    with pytest.raises(Exception, match="invalid network name"):
        config.NetworkConfig(name="..")


def test_ordinary_network_names_still_validate():
    for name in ("lab", "dgx-site", "lab_2", "site.one", "A1"):
        assert config.NetworkConfig(name=name).name == name


def test_a_retired_bundle_compress_key_loads_and_is_dropped_on_save(tmp_path):
    """Bundles are plain tars now; a config that still says
    `bundle_compress = false` must keep loading, and lose the key on save."""
    (tmp_path / "mmw.toml").write_text(
        '[settings]\nmirror_dir = "mirror"\n\n'
        '[[networks]]\nname = "lab"\nbundle_compress = false\n')
    cfg = config.load(tmp_path)
    assert [n.name for n in cfg.networks] == ["lab"]
    config.save(cfg, tmp_path)
    assert "bundle_compress" not in (tmp_path / "mmw.toml").read_text()


def test_max_connections_must_be_at_least_one():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        config.Settings(mirror_dir="/m", max_connections=0)


def test_save_replaces_the_file_whole_and_keeps_a_symlink(tmp_path, monkeypatch):
    """A crash mid-write used to leave a truncated mmw.toml that no command
    would load. The write is staged and renamed — onto the symlink's target,
    so a symlinked config stays a symlink."""
    real = tmp_path / "real" / "mmw.toml"
    real.parent.mkdir()
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"))
    config.save(cfg, real.parent)
    real.chmod(0o600)
    link_dir = tmp_path / "cwd"
    link_dir.mkdir()
    (link_dir / "mmw.toml").symlink_to(real)

    config.save(cfg.model_copy(update={"settings": config.Settings(
        mirror_dir=tmp_path / "m", max_connections=3)}), link_dir)
    assert (link_dir / "mmw.toml").is_symlink()
    assert config.load(real.parent).settings.max_connections == 3
    assert real.stat().st_mode & 0o777 == 0o600
    assert not list(real.parent.glob("*.tmp"))

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(config.tomli_w, "dump", boom)
    with pytest.raises(OSError):
        config.save(cfg, real.parent)
    assert config.load(real.parent).settings.max_connections == 3     # untouched
