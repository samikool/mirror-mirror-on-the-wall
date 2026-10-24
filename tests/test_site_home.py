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


def test_cli_init_prints_export(tmp_path):
    r = CliRunner().invoke(cli.app, ["site", "init", "--root", str(tmp_path / "root")])
    assert r.exit_code == 0, r.output
    assert f"export MMW_SITE_HOME={tmp_path / 'root' / '.mmw' / 'site'}" in r.output
