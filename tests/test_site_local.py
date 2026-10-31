"""A site serving folder repositories from a separate mmw instance it only reads."""
import asyncio
import concurrent.futures
import hashlib
import shutil

import pytest

from debs import make_deb
from mmw.core import config
from mmw.core.pool import Pool, database_path
from mmw.core.signing import Keyring
from mmw.core.sync_engine import KeySyncCoordinator, sync_mirror
from mmw.providers import get_provider
from mmw.providers.base import ResolveContext
from mmw.site.db import SiteDB
from test_layers import lab  # noqa: F401
from test_site_layers import site  # noqa: F401

KEY = config.normalize_url("https://tools.lab/apt")

pytestmark = pytest.mark.skipif(shutil.which("gpg") is None or shutil.which("gpgv") is None,
                                reason="needs gpg and gpgv")


def _sync_instance(inst):
    cfg = config.load(inst)
    src = cfg.sources[0]
    resolved = get_provider("apt-local").resolve(src, context=ResolveContext.from_config(cfg, config_dir=inst))
    assert not resolved.errors, resolved.errors
    md = cfg.settings.mirror_dir
    with Pool(md) as pool, concurrent.futures.ThreadPoolExecutor(2) as ex:
        for unit in resolved.mirrors:
            assert asyncio.run(sync_mirror(unit, pool=pool, mirror_dir=md, max_connections=2, executor=ex,
                                           keys=KeySyncCoordinator(md))).ok


@pytest.fixture
def instance(tmp_path):
    """An mmw instance (its own folder) with a synced folder repository in network `local`."""
    inst = tmp_path / "instance"
    inst.mkdir()
    make_deb(inst / "debs", "ours", "1.0")
    src = config.LocalAptSource(provider="apt-local", url="https://tools.lab/apt", folder="debs", key="tools")
    cfg = config.Config(settings=config.Settings(mirror_dir=inst / "pool"), sources=[src],
                        networks=[config.NetworkConfig(name="local")])
    config.save(cfg, inst)
    Keyring(inst / "pool").generate("tools")
    _sync_instance(inst)
    return inst


def _digest_tree(path):
    return {p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(path.rglob("*")) if p.is_file() and not p.is_symlink()}


def test_a_local_instance_is_served_by_the_site(site, instance):  # noqa: F811
    from mmw.site import local
    before_instance = _digest_tree(instance)
    connected_nginx = (site.root / "nginx" / "mirror.conf").read_bytes()
    local.add(site.home, instance, "local")
    [result] = local.update(site.home)
    assert result.error is None, result
    tree = site.root / "tools.lab" / "apt"
    assert (tree / "ours_1.0_amd64.deb").is_file() and b"Package: ours" in (tree / "Packages").read_bytes()
    assert b"server_name tools.lab" in (site.root / "nginx" / "local-local.conf").read_bytes()
    assert list((site.root / "keys").rglob("tools.gpg"))
    assert (site.root / "nginx" / "mirror.conf").read_bytes() == connected_nginx   # the connected network's
    with SiteDB(site.home) as db:
        assert any(s.repo == KEY for s in db.repo_states())
        assert any(s.repo == site.repo for s in db.repo_states())                  # still there
    assert _digest_tree(instance) == before_instance                               # read only


def test_an_unchanged_instance_stores_nothing_new(site, instance):  # noqa: F811
    from mmw.site import local
    local.add(site.home, instance, "local")
    local.update(site.home)
    [again] = local.update(site.home)
    assert again.error is None and again.updated == []
    with SiteDB(site.home) as db:
        assert len(db.versions(KEY)) == 1
    make_deb(instance / "debs", "more", "1.0")
    _sync_instance(instance)
    [changed] = local.update(site.home)
    assert changed.updated == [KEY]
    with SiteDB(site.home) as db:
        assert [v.number for v in db.versions(KEY)] == [2, 1]


def test_a_busy_instance_is_skipped(site, instance):  # noqa: F811
    from mmw.site import local
    local.add(site.home, instance, "local")
    wal = database_path(instance / "pool").with_name(database_path(instance / "pool").name + "-wal")
    wal.write_bytes(b"x" * 100)
    [result] = local.update(site.home)
    assert result.error and "busy" in result.error
    with SiteDB(site.home) as db:
        assert not any(s.repo == KEY for s in db.repo_states())


def test_add_checks_the_instance(site, tmp_path):  # noqa: F811
    from mmw.site import local
    with pytest.raises(ValueError, match="no mmw"):
        local.add(site.home, tmp_path / "nothing", "local")


def test_site_local_cli(site, instance):  # noqa: F811
    from typer.testing import CliRunner
    from mmw.cli import app
    run = lambda *a: CliRunner().invoke(app, ["site", "local", *a, "--root", str(site.root)])  # noqa: E731
    assert run("add", str(instance), "-n", "local").exit_code == 0
    assert "local" in run("list").output
    updated = run("update")
    assert updated.exit_code == 0 and "1 updated" in updated.output
    assert "unchanged" in run("update").output
    assert run("remove", "local").exit_code == 0
    assert "No local instances" in run("list").output


def test_the_first_update_says_to_reload_nginx(site, instance):  # noqa: F811
    from mmw.site import local
    local.add(site.home, instance, "local")
    [first] = local.update(site.home)
    assert any("reload nginx" in line for line in first.lines)
    [again] = local.update(site.home)
    assert not any("reload nginx" in line for line in again.lines)


def test_a_local_repository_cannot_take_a_connected_one(site, tmp_path):  # noqa: F811
    from mmw.site import local
    inst = tmp_path / "clash"
    inst.mkdir()
    make_deb(inst / "debs", "x", "1")
    src = config.LocalAptSource(provider="apt-local", url=site.lab.src.url, folder="debs", key="tools")
    config.save(config.Config(settings=config.Settings(mirror_dir=inst / "pool"), sources=[src],
                              networks=[config.NetworkConfig(name="clash")]), inst)
    Keyring(inst / "pool").generate("tools")
    _sync_instance(inst)
    local.add(site.home, inst, "clash")
    [result] = local.update(site.home)
    assert result.error and "another network" in result.error


def test_immutable_reads_nest(tmp_path):
    from mmw.core.pool import _IMMUTABLE, immutable_reads
    with immutable_reads(tmp_path):
        with immutable_reads(tmp_path):
            pass
        assert str(tmp_path.resolve()) in _IMMUTABLE
        with pytest.raises(RuntimeError):
            Pool(tmp_path)
    assert str(tmp_path.resolve()) not in _IMMUTABLE
