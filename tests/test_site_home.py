"""A far-side site: a home holding its state, a root it serves, one lock."""
import pytest
from typer.testing import CliRunner

from mmw import cli
from mmw.site import home as H
from mmw.site.lock import LockHeld, SiteLock, break_lock


def test_init_and_load_round_trip(tmp_path):
    s = H.init_site(tmp_path / "h", tmp_path / "root")
    assert s.root == tmp_path / "root" and s.keep_versions is None
    assert (tmp_path / "root/.mmw/store").is_dir()
    assert H.load_site(tmp_path / "h") == s


def test_init_refuses_existing(tmp_path):
    H.init_site(tmp_path / "h", tmp_path / "root")
    with pytest.raises(FileExistsError):
        H.init_site(tmp_path / "h", tmp_path / "root")


def test_load_missing_says_init(tmp_path):
    with pytest.raises(FileNotFoundError, match="mmw site init"):
        H.load_site(tmp_path / "nope")


def test_resolve_home_env_and_error(tmp_path, monkeypatch):
    monkeypatch.delenv("MMW_SITE_HOME", raising=False)
    assert H.resolve_home(tmp_path) == tmp_path
    with pytest.raises(ValueError, match="MMW_SITE_HOME"):
        H.resolve_home(None)
    monkeypatch.setenv("MMW_SITE_HOME", str(tmp_path / "e"))
    assert H.resolve_home(None) == tmp_path / "e"


def test_lock_refuses_second_holder(tmp_path):
    (tmp_path / "h").mkdir()
    with SiteLock(tmp_path / "h"):
        with pytest.raises(LockHeld) as e:
            with SiteLock(tmp_path / "h"):
                pass
        import os
        assert str(os.getpid()) in e.value.holder
    with SiteLock(tmp_path / "h"):                       # released on exit
        pass
    (tmp_path / "h" / "lock").write_text("other 1 2026-01-01T00:00:00\n")   # a stale one
    with pytest.raises(LockHeld):
        with SiteLock(tmp_path / "h"):
            pass
    assert break_lock(tmp_path / "h") is True and break_lock(tmp_path / "h") is False
    with SiteLock(tmp_path / "h"):
        pass


def test_cli_rejects_init(tmp_path):
    """No init: the first apply makes the roots it names."""
    assert CliRunner().invoke(cli.app, ["site", "init", "--root", str(tmp_path / "root")]).exit_code != 0
    assert " init " not in CliRunner().invoke(cli.app, ["site", "--help"]).output


# ── v2: every root its own site ──────────────────────────────────────────────

def test_init_root_records_identity(tmp_path):
    r = tmp_path / "srv"
    home = H.init_root(r, name="nas", set_id="s1", network="debian")
    info = H.read_root(str(r))
    assert home == r / ".mmw" / "site" == info.home
    assert (info.name, info.set_id, info.network, info.members) == ("nas", "s1", "debian", ("nas",))


def test_init_root_refuses_another_sets_root(tmp_path):
    r = tmp_path / "srv"
    H.init_root(r, name="nas", set_id="s1", network="debian")
    with pytest.raises(ValueError, match="debian"):
        H.init_root(r, name="nas", set_id="s2", network="ubuntu")


def test_read_root_of_a_plain_directory_is_none(tmp_path):
    assert H.read_root(str(tmp_path)) is None
    assert H.read_root(str(tmp_path / "not-there")) is None


def test_a_root_is_found_where_it_is_mounted(tmp_path):
    """Paths don't travel: the same share mounted elsewhere is the same root, at its new path."""
    import shutil
    r = tmp_path / "a"
    H.init_root(r, name="nas", set_id="s1", network="debian")
    shutil.move(str(r), str(tmp_path / "b"))
    assert H.load_site(H.root_home(tmp_path / "b")).root == (tmp_path / "b").resolve()


def test_hostnames_from_bases(tmp_path):
    from mmw.site.db import SiteDB
    r = tmp_path / "srv"
    home = H.init_root(r, name="nas", set_id="s1", network="debian")
    with SiteDB(home) as db:
        db.add_version("k", "debian", 1, "d", "20261025T000000Z", None, ["deb.debian.org/debian"], [])
    assert H.hostnames(home) == {"deb.debian.org"}
