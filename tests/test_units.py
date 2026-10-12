"""MirrorConfig as a generic unit: coordinates in `dims`, provider settings in
`options`, the old apt field names still accepted and readable."""
from mmw.core.config import MirrorConfig, Unit


def test_legacy_kwargs_fold_into_dims_and_options():
    m = MirrorConfig(name="u-noble-main-amd64", provider="apt", source_url="https://x/u",
                     arch="amd64", suite="noble", component="main",
                     include_sources=True, gpg_key_url="https://x/k.asc")
    assert m.dims == {"arch": "amd64", "suite": "noble", "component": "main"}
    assert m.options == {"include_sources": True, "gpg_key_url": "https://x/k.asc"}
    assert (m.arch, m.suite, m.component) == ("amd64", "noble", "main")
    assert m.include_sources is True and m.gpg_key_url == "https://x/k.asc"


def test_defaults_match_the_old_fields():
    m = MirrorConfig(name="f", provider="apt-flat", source_url="https://x/f")
    assert m.arch == "amd64"                 # old `arch: str = "amd64"`
    assert m.suite is None and m.component is None
    assert m.include_sources is False and m.dist_upgrade is False
    assert m.gpg_key is None and m.gpg_key_name is None and m.gpg_key_url is None
    assert m.extra_files == []


def test_unknown_arch_is_kept_empty_not_defaulted():
    m = MirrorConfig(name="f", provider="apt-flat", source_url="https://x/f", arch="")
    assert m.arch == ""


def test_option_assignment_sticks():
    m = MirrorConfig(name="u", provider="apt", source_url="https://x/u")
    m.gpg_key = "ARMOUR"
    m.gpg_key_name = "stem"
    m.gpg_key_url = "https://x/key.asc"
    assert (m.gpg_key, m.gpg_key_name, m.gpg_key_url) == ("ARMOUR", "stem", "https://x/key.asc")
    assert m.options["gpg_key"] == "ARMOUR"


def test_dump_round_trips_and_old_dumps_still_load():
    m = MirrorConfig(name="u", provider="apt", source_url="https://x/u", suite="noble",
                     component="main", arch="arm64", dist_upgrade=True)
    assert MirrorConfig.model_validate(m.model_dump(mode="json")) == m
    old = {"name": "u", "provider": "apt", "source_url": "https://x/u", "source": "",
           "label": "", "arch": "arm64", "suite": "noble", "component": "main",
           "include_sources": False, "gpg_key_url": None, "gpg_key": None,
           "gpg_key_name": None, "extra_files": [], "dist_upgrade": True}
    loaded = MirrorConfig.model_validate(old)
    # `options` differ in which defaults are spelled out; what they mean must not.
    assert loaded.dims == m.dims
    assert (loaded.dist_upgrade, loaded.include_sources, loaded.gpg_key, loaded.extra_files) == \
        (True, False, None, [])


def test_unit_alias():
    assert Unit is MirrorConfig
