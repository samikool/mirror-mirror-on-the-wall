"""Moving the pool database: copy, verify, switch config, remove the old one."""
import hashlib
import subprocess
import sys

import pytest

from mmw.core import config
from mmw.core.database_move import DatabaseMoveError, move_database
from mmw.core.pool import Pool, database_path


@pytest.fixture
def setup(tmp_path):
    md = tmp_path / "mirror"
    (tmp_path / "mmw.toml").write_text(f'[settings]\nmirror_dir = "{md}"\n')
    cfg = config.load(tmp_path)
    with Pool(md, defer_writes=False) as pool:
        cs = hashlib.sha256(b"x").hexdigest()
        pool.add(b"x", cs, "sha256", "x.deb", 1)
        pool.reconcile_refs("m", [(cs, "pool/x.deb", "pkg")])
    return tmp_path, cfg, md


def _rows(md):
    with Pool(md, read_only=True) as pool:
        return pool.mirror_files(["m"])


def test_move_switches_the_config_and_removes_the_old_database(setup, tmp_path):
    cfg_dir, cfg, md = setup
    before = _rows(md)
    old = md / ".pool" / "mmw.db"
    dest = tmp_path / "fast" / "mmw.db"
    seen = []
    new_cfg = move_database(cfg_dir, cfg, dest, progress=lambda done, total: seen.append(done))
    assert new_cfg.settings.database == dest
    assert config.load(cfg_dir).settings.database == dest
    assert database_path(md) == dest
    assert dest.exists() and not old.exists()
    assert not old.with_name("mmw.db-wal").exists() and not old.with_name("mmw.db-shm").exists()
    assert _rows(md) == before
    assert seen and seen[-1] == dest.stat().st_size


def test_move_is_refused_while_a_sync_holds_the_pool(setup, tmp_path):
    cfg_dir, cfg, md = setup
    held = subprocess.Popen([sys.executable, "-c",
                             "import sys, time; from pathlib import Path; "
                             "from mmw.core.pool import acquire_sync_lock; "
                             f"acquire_sync_lock(Path({str(md)!r})); print('held', flush=True); "
                             "time.sleep(30)"], stdout=subprocess.PIPE, text=True)
    try:
        assert held.stdout.readline().strip() == "held"
        with pytest.raises(DatabaseMoveError, match="sync"):
            move_database(cfg_dir, cfg, tmp_path / "fast" / "mmw.db")
    finally:
        held.kill()
    assert config.load(cfg_dir).settings.database == md / ".pool" / "mmw.db"
    assert (md / ".pool" / "mmw.db").exists()


def test_move_into_a_read_only_directory_changes_nothing(setup, tmp_path):
    cfg_dir, cfg, md = setup
    locked = tmp_path / "ro"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        with pytest.raises(DatabaseMoveError):
            move_database(cfg_dir, cfg, locked / "mmw.db")
    finally:
        locked.chmod(0o700)
    assert list(locked.iterdir()) == []
    assert config.load(cfg_dir).settings.database == md / ".pool" / "mmw.db"


def test_a_copy_that_fails_midway_leaves_the_old_database_in_force(setup, tmp_path, monkeypatch):
    cfg_dir, cfg, md = setup
    import mmw.core.database_move as dm

    def broken(src, dst, progress):
        dst.write_bytes(b"partial")
        raise OSError("disk full")
    monkeypatch.setattr(dm, "_copy", broken)
    dest = tmp_path / "fast" / "mmw.db"
    with pytest.raises(DatabaseMoveError, match="disk full"):
        move_database(cfg_dir, cfg, dest)
    assert not dest.exists() and not dest.with_name("mmw.db.tmp").exists()
    assert (md / ".pool" / "mmw.db").exists()
    assert config.load(cfg_dir).settings.database == md / ".pool" / "mmw.db"
    assert _rows(md)


def test_an_existing_destination_is_refused(setup, tmp_path):
    cfg_dir, cfg, md = setup
    dest = tmp_path / "taken.db"
    dest.write_bytes(b"someone else's")
    with pytest.raises(DatabaseMoveError, match="already exists"):
        move_database(cfg_dir, cfg, dest)
    assert dest.read_bytes() == b"someone else's"


def test_the_cli_moves_it(setup, tmp_path):
    cfg_dir, _cfg, md = setup
    dest = tmp_path / "fast" / "mmw.db"
    from typer.testing import CliRunner

    from mmw import cli
    run = CliRunner().invoke(cli.app, ["--config", str(cfg_dir), "move-database", str(dest)])
    assert run.exit_code == 0, run.output
    assert config.load(cfg_dir).settings.database == dest


@pytest.mark.parametrize("job", ["publish", "bundle"])
def test_move_is_refused_while_a_publish_or_bundle_runs(job, seeded, tmp_path):  # noqa: F811
    from mmw.core.config import NetworkConfig
    from mmw.core.database_move import DatabaseMoveError, move_database
    from mmw.core.publisher import publish_network
    from mmw.core.publisher.bundle import bundle_network
    mirror_dir, cfg = seeded
    net = NetworkConfig(name="lab", root=tmp_path / "pub", bundle_dir=tmp_path / "b", nginx=False)
    cfg = cfg.model_copy(update={"networks": [net]})
    config.save(cfg, tmp_path)
    cfg = config.load(tmp_path)
    tried = []

    def progress(rel, status):
        if not tried:
            with pytest.raises(DatabaseMoveError):
                move_database(tmp_path, cfg, tmp_path / "fast" / "mmw.db")
            tried.append(1)
    if job == "publish":
        publish_network(net, cfg, mirror_dir, progress=progress)
    else:
        bundle_network(net, cfg, mirror_dir, full=True, progress=progress)
    assert tried
    assert config.load(tmp_path).settings.database == mirror_dir / ".pool" / "mmw.db"


from test_bundle import seeded  # noqa: E402,F401
