"""The `apt-local` provider: a folder of debs, signed and served at a URL."""
import hashlib
from dataclasses import replace

import pytest
from pydantic import ValidationError

from debs import make_deb
from mmw.core import config, downloader
from mmw.core.downloader import Status
from mmw.core.pool import Pool
from mmw.providers import get_provider
from mmw.providers.base import PackageInfo


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


def test_a_file_url_is_pooled_and_verified(tmp_path):
    deb = make_deb(tmp_path, "a", "1")
    digest = hashlib.sha256(deb.read_bytes()).hexdigest()
    pkg = PackageInfo("a", deb.as_uri(), digest, "sha256", deb.stat().st_size, "a_1_amd64.deb", deb.name)
    chunks = []
    with Pool(tmp_path / "m") as pool:
        result = downloader._download_sync(pkg, pool, chunks.append, None)
        assert result.status is Status.DOWNLOADED and pool.contains(digest)
        assert sum(chunks) == deb.stat().st_size
        bad = downloader._download_sync(replace(pkg, checksum="0" * 64), pool, None, None)
        assert bad.status is Status.FAILED and bad.error == "checksum mismatch"
    missing = replace(pkg, url=(tmp_path / "gone.deb").as_uri())
    with Pool(tmp_path / "m") as pool, pytest.raises(downloader.PermanentFetchError, match="not found"):
        downloader._download_sync(missing, pool, None, None)
