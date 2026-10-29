"""The `apt-local` provider: a folder of debs, signed and served at a URL."""
import pytest
from pydantic import ValidationError

from mmw.core import config
from mmw.providers import get_provider


def test_local_source_round_trips_through_toml(tmp_path):
    src = config.LocalAptSource(provider="apt-local", url="https://tools.lab/apt",
                                folder="debs", key="tools")
    assert config.source_to_dict(src) == {"provider": "apt-local", "url": "https://tools.lab/apt",
                                          "folder": "debs", "key": "tools"}
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path), sources=[src])
    config.save(cfg, tmp_path)
    back = config.load(tmp_path).sources[0]
    assert back == src
    assert back.folder_path(tmp_path) == tmp_path / "debs"
    assert back.folder_path(None) == tmp_path.resolve() / "debs"     # where it was loaded from
    assert (back.structured, back.suite, back.component, back.contents) == (False, "stable", "main", False)


def test_non_default_fields_are_written():
    src = config.LocalAptSource(provider="apt-local", url="https://tools.lab/apt", folder="/d",
                                key="k", structured=True, suite="jammy", component="tools",
                                contents=True, name="ours")
    d = config.source_to_dict(src)
    assert {k: d[k] for k in ("structured", "suite", "component", "contents", "name")} == {
        "structured": True, "suite": "jammy", "component": "tools", "contents": True, "name": "ours"}


def test_expand_is_one_unit_at_the_served_url():
    src = config.LocalAptSource(provider="apt-local", url="https://tools.lab/apt", folder="/d",
                                key="k", structured=True)
    provider = get_provider("apt-local")
    [unit] = provider.expand(src)
    assert unit.mirror_base == "tools.lab/apt"
    assert unit.dims == {"suite": "stable", "component": "main"}
    assert unit.options["folder"] == "/d" and unit.options["key"] == "k"
    assert provider.covers(src, unit)
    flat = src.model_copy(update={"structured": False})
    assert provider.expand(flat)[0].dims == {}


def test_bad_url_and_names_are_refused():
    for bad in ({"url": "file:///x"}, {"suite": "a b"}, {"component": "../x"}):
        with pytest.raises(ValidationError):
            config.LocalAptSource(**{"provider": "apt-local", "url": "https://h/p",
                                     "folder": "/d", "key": "k", **bad})
