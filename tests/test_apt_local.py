"""The `apt-local` provider: a folder of debs, signed and served at a URL."""
import asyncio
import concurrent.futures
import hashlib
import shutil
from dataclasses import replace

import pytest
from pydantic import ValidationError

from debs import make_deb
from mmw.core import config, downloader
from mmw.core.downloader import Status
from mmw.core.pool import Pool
from mmw.core.publisher import publish_network
from mmw.core.signing import Keyring
from mmw.core.sync_engine import KeySyncCoordinator, sync_mirror
from mmw.providers import apt_local, get_provider
from mmw.providers.base import PackageInfo, ResolveContext
from test_signing import gpgv_ok

needs_gpg = pytest.mark.skipif(shutil.which("gpg") is None or shutil.which("gpgv") is None,
                               reason="needs gpg and gpgv")


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


# ── sync, build and sign ─────────────────────────────────────────────────────

def _src(tmp_path, folder="debs", **kw):
    return config.LocalAptSource(provider="apt-local", url="https://tools.lab/apt",
                                 folder=str(tmp_path / folder), key="tools", **kw)


def _sync_local(tmp_path, src):
    """Resolve, sync and publish one folder repository; (unit, summary, root)."""
    mirror_dir = tmp_path / "m"
    kr = Keyring(mirror_dir)
    if not any(k.name == "tools" for k in kr.list()):
        kr.generate("tools")
    net = config.NetworkConfig(name="lab", root=tmp_path / "pub")
    cfg = config.Config(settings=config.Settings(mirror_dir=mirror_dir), sources=[src], networks=[net])
    resolved = get_provider("apt-local").resolve(src, context=ResolveContext.from_config(cfg))
    assert not resolved.errors, resolved.errors
    [unit] = resolved.mirrors
    with Pool(mirror_dir) as pool, concurrent.futures.ThreadPoolExecutor(2) as ex:
        summary = asyncio.run(sync_mirror(unit, pool=pool, mirror_dir=mirror_dir, max_connections=2,
                                          executor=ex, keys=KeySyncCoordinator(mirror_dir)))
    publish_network(net, cfg, mirror_dir)
    return unit, summary, tmp_path / "pub"


def _metadata_bytes(tmp_path, name="tools"):
    with Pool(tmp_path / "m", read_only=True) as pool:
        return {rel: open(path, "rb").read() for rel, path, *_ in pool.mirror_files([name])
                if not rel.endswith(".deb")}


@needs_gpg
def test_flat_folder_syncs_signed_and_publishes(tmp_path):
    make_deb(tmp_path / "debs", "tool", "1.0")
    make_deb(tmp_path / "debs", "doc", "1.0", "all")
    unit, summary, root = _sync_local(tmp_path, _src(tmp_path))
    assert summary.ok and summary.downloaded == 2
    base = root / "tools.lab/apt"
    assert {"Packages", "Packages.gz", "Packages.xz", "Release", "InRelease", "Release.gpg",
            "tool_1.0_amd64.deb", "doc_1.0_all.deb"} <= {p.name for p in base.iterdir()}
    assert "Filename: ./tool_1.0_amd64.deb" in (base / "Packages").read_text()
    assert (base / f"{unit.name}.sources").read_text().startswith(f"# {unit.name} — served by mmw")
    assert "./\n" in (base / f"{unit.name}.list").read_text()
    release = (base / "Release").read_text()
    assert f"{unit.name}.list" not in release
    assert "Architectures: amd64\n" in release and "Suite:" not in release
    assert gpgv_ok(tmp_path, (root / f"keys/{unit.label}/tools.asc").read_text(),
                   (base / "Release.gpg").read_bytes(), (base / "Release").read_bytes())


@needs_gpg
def test_structured_puts_all_debs_in_every_arch(tmp_path):
    for arch in ("amd64", "arm64"):
        make_deb(tmp_path / "debs", "tool", "1.0", arch)
    make_deb(tmp_path / "debs", "doc", "1.0", "all")
    _, _, root = _sync_local(tmp_path, _src(tmp_path, structured=True))
    dists = root / "tools.lab/apt/dists/stable"
    for arch in ("amd64", "arm64"):
        text = (dists / f"main/binary-{arch}/Packages").read_text()
        assert "Package: doc" in text and f"Architecture: {arch}" in text
        assert "Filename: pool/main/doc_1.0_all.deb" in text
    release = (dists / "Release").read_text()
    assert "Codename: stable" in release and "Components: main" in release
    assert " main/binary-arm64/Packages.xz" in release
    assert (root / "tools.lab/apt/pool/main/tool_1.0_arm64.deb").is_file()


@needs_gpg
def test_resync_unchanged_is_byte_identical_and_rereads_nothing(tmp_path, monkeypatch):
    make_deb(tmp_path / "debs", "tool", "1.0")
    _sync_local(tmp_path, _src(tmp_path))
    first = _metadata_bytes(tmp_path)
    assert "InRelease" in first
    monkeypatch.setattr(apt_local, "read_control", lambda p: pytest.fail("re-read " + p.name))
    _sync_local(tmp_path, _src(tmp_path))
    assert _metadata_bytes(tmp_path) == first


@needs_gpg
def test_contents_only_when_asked(tmp_path):
    make_deb(tmp_path / "debs", "tool", "1.0")
    _, _, root = _sync_local(tmp_path, _src(tmp_path))
    assert not (root / "tools.lab/apt/Contents-amd64").exists()
    _, _, root = _sync_local(tmp_path, _src(tmp_path, contents=True))
    assert "usr/share/doc/tool/README  utils/tool" in (root / "tools.lab/apt/Contents-amd64").read_text()
    assert "Contents-amd64.gz" in (root / "tools.lab/apt/Release").read_text()


@needs_gpg
def test_missing_empty_and_clashing_folders(tmp_path):
    with pytest.raises(ValueError, match=str(tmp_path / "nope")):
        apt_local.scan(tmp_path / "nope", tmp_path / "c.json")
    (tmp_path / "empty").mkdir()
    _, summary, root = _sync_local(tmp_path, _src(tmp_path, folder="empty"))
    assert summary.ok and (root / "tools.lab/apt/Packages").read_bytes() == b""
    make_deb(tmp_path / "a", "x", "1")
    make_deb(tmp_path / "a" / "sub", "x", "1", extra={"Depends": "y"}, filename="x_1_amd64-rebuilt.deb")
    with pytest.raises(ValueError, match="differ") as clash:
        apt_local.scan(tmp_path / "a", tmp_path / "c2.json")
    assert "sub/x_1_amd64-rebuilt.deb and x_1_amd64.deb" in str(clash.value)


def test_a_missing_folder_is_a_resolve_error(tmp_path):
    src = _src(tmp_path, folder="nope")
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"), sources=[src])
    resolved = get_provider("apt-local").resolve(src, context=ResolveContext.from_config(cfg))
    assert resolved.mirrors == [] and str(tmp_path / "nope") in resolved.errors[0]


# ── review fixes ─────────────────────────────────────────────────────────────

def _release_date(root, structured=False):
    from email.utils import parsedate_to_datetime
    rel = root / ("tools.lab/apt/dists/stable/Release" if structured else "tools.lab/apt/Release")
    line = next(ln for ln in rel.read_text().splitlines() if ln.startswith("Date: "))
    return parsedate_to_datetime(line[6:].replace("UTC", "+0000"))


@needs_gpg
def test_date_never_goes_back_when_the_newest_deb_is_removed(tmp_path):
    """A client that saw a newer Date ignores an older one: removing the
    newest deb must still move Date forward, or clients keep a stale list."""
    from debs import touch_old
    touch_old(make_deb(tmp_path / "debs", "tool", "1.0"), 7200)
    newest = make_deb(tmp_path / "debs", "tool", "1.1")
    _, _, root = _sync_local(tmp_path, _src(tmp_path))
    first = _release_date(root)
    newest.unlink()
    _, _, root = _sync_local(tmp_path, _src(tmp_path))
    assert _release_date(root) > first


@needs_gpg
def test_a_future_mtime_does_not_date_the_release_ahead(tmp_path):
    import os
    import time
    deb = make_deb(tmp_path / "debs", "tool", "1.0")
    later = time.time() + 86400
    os.utime(deb, (later, later))
    _, _, root = _sync_local(tmp_path, _src(tmp_path))
    assert _release_date(root).timestamp() <= time.time() + 5


@pytest.mark.skipif(os_getuid() == 0 if (os_getuid := __import__("os").getuid) else False,
                    reason="root reads everything")
def test_unreadable_folder_and_subfolder_are_errors(tmp_path):
    folder = tmp_path / "f"
    make_deb(folder, "a", "1")
    make_deb(folder / "sub", "b", "1")
    (folder / "sub").chmod(0)
    try:
        with pytest.raises(ValueError, match="sub"):
            apt_local.scan(folder, tmp_path / "c.json")
    finally:
        (folder / "sub").chmod(0o755)
    folder.chmod(0)
    try:
        with pytest.raises(ValueError, match="cannot be read"):
            apt_local.scan(folder, tmp_path / "c.json")
    finally:
        folder.chmod(0o755)


def test_the_same_file_name_twice_is_refused(tmp_path):
    make_deb(tmp_path / "f" / "a", "alpha", "1", filename="tool.deb")
    make_deb(tmp_path / "f" / "b", "beta", "1", filename="tool.deb")
    with pytest.raises(ValueError) as clash:
        apt_local.scan(tmp_path / "f", tmp_path / "c.json")
    assert "a/tool.deb" in str(clash.value) and "b/tool.deb" in str(clash.value)


@needs_gpg
def test_an_emptied_structured_repo_keeps_its_architectures(tmp_path):
    deb = make_deb(tmp_path / "debs", "tool", "1.0", "arm64")
    _sync_local(tmp_path, _src(tmp_path, structured=True))
    deb.unlink()
    _, _, root = _sync_local(tmp_path, _src(tmp_path, structured=True))
    dists = root / "tools.lab/apt/dists/stable"
    assert "Architectures: arm64\n" in (dists / "Release").read_text()
    assert (dists / "main/binary-arm64/Packages").read_bytes() == b""


def test_reading_a_large_deb_does_not_load_it_whole(tmp_path):
    import os
    import tracemalloc
    from debs import make_deb_with_data
    deb = make_deb_with_data(tmp_path, "big", "1", os.urandom(24 * 1024 * 1024), compression="zst")
    tracemalloc.start()
    from mmw.core.apt_index import data_paths, read_control
    assert read_control(deb)["Package"] == "big"
    assert data_paths(deb) == ["usr/share/big/blob"]
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert peak < 8 * 1024 * 1024, f"peak {peak / 1e6:.0f} MB for a 24 MB deb"
