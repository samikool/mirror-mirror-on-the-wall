"""`settings.database` places the pool database; mmw opens it only there."""
import asyncio
import hashlib
from pathlib import Path

import pytest

from mmw.core import config
from mmw.core import pool as pool_mod
from mmw.core.pool import Pool, PoolDatabaseMissing, database_path, register_database


def _write(cfg_dir: Path, settings: str) -> None:
    (cfg_dir / "mmw.toml").write_text(f"[settings]\n{settings}\n")


def test_a_config_without_database_defaults_to_the_pool_and_saves_it(tmp_path):
    md = tmp_path / "mirror"
    _write(tmp_path, f'mirror_dir = "{md}"')
    cfg = config.load(tmp_path)
    assert cfg.settings.database == md / ".pool" / "mmw.db"
    config.save(cfg, tmp_path)
    assert f'database = "{md / ".pool" / "mmw.db"}"' in (tmp_path / "mmw.toml").read_text()


def test_the_pool_opens_the_configured_database(tmp_path):
    md, db = tmp_path / "mirror", tmp_path / "fast" / "pool.db"
    db.parent.mkdir()
    _write(tmp_path, f'mirror_dir = "{md}"\ndatabase = "{db}"')
    config.load(tmp_path)
    assert database_path(md) == db
    with Pool(md) as pool:
        files = {r[2] for r in pool._conn.execute("PRAGMA database_list")}
    assert str(db) in files
    assert not (md / ".pool" / "mmw.db").exists()


def _pool_with_a_blob(md: Path) -> None:
    with Pool(md, defer_writes=False) as pool:
        pool.add(b"x", hashlib.sha256(b"x").hexdigest(), "sha256", "x.deb", 1)


@pytest.mark.parametrize("read_only", [False, True])
def test_a_missing_database_over_an_existing_pool_is_refused(tmp_path, read_only):
    md = tmp_path / "mirror"
    _pool_with_a_blob(md)
    elsewhere = tmp_path / "nowhere" / "mmw.db"
    register_database(md, elsewhere)
    try:
        with pytest.raises(PoolDatabaseMissing, match=str(elsewhere)):
            Pool(md, read_only=read_only)
        assert not elsewhere.exists()
    finally:
        register_database(md, md / ".pool" / "mmw.db")


def test_a_brand_new_pool_still_initialises_at_the_configured_path(tmp_path):
    md, db = tmp_path / "mirror", tmp_path / "fast" / "mmw.db"
    db.parent.mkdir()
    register_database(md, db)
    with Pool(md):
        pass
    assert db.exists()


def _settings_app(tmp_path, settings: str):
    from mmw.tui.app import MmwApp
    _write(tmp_path, settings)
    return MmwApp(config.load(tmp_path), tmp_path)


def test_moving_the_pool_directory_moves_a_default_database_setting(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    app = _settings_app(tmp_path, f'mirror_dir = "{old}"')

    async def go():
        async with app.run_test():
            await app.save_settings(mirror_dir=new)
    asyncio.run(go())
    assert config.load(tmp_path).settings.database == new / ".pool" / "mmw.db"
    assert database_path(new) == new / ".pool" / "mmw.db"


def test_moving_the_pool_directory_keeps_a_placed_database(tmp_path):
    old, new, db = tmp_path / "old", tmp_path / "new", tmp_path / "fast" / "mmw.db"
    app = _settings_app(tmp_path, f'mirror_dir = "{old}"\ndatabase = "{db}"')

    async def go():
        async with app.run_test():
            await app.save_settings(mirror_dir=new)
    asyncio.run(go())
    assert config.load(tmp_path).settings.database == db
    assert database_path(new) == db


def test_registry_default_is_the_pool_location(tmp_path):
    assert pool_mod.database_path(tmp_path / "x") == tmp_path / "x" / ".pool" / "mmw.db"


def test_nothing_builds_the_default_database_path_itself():
    """Every opener asks `database_path()`; a hand-built `.pool/mmw.db` reads
    an empty pool once the database has moved (a snapshot publish then
    pruned a whole tree)."""
    import re
    src = Path(__file__).resolve().parents[1] / "src" / "mmw"
    hits = [f"{p.relative_to(src)}:{n}" for p in src.rglob("*.py")
            for n, line in enumerate(p.read_text().splitlines(), 1)
            if re.search(r'"mmw\.db"', line) and "def default_database" not in line
            and not (p.name == "pool.py" and 'return Path(mirror_dir) / ".pool" / "mmw.db"' in line)]
    assert not hits, hits


def test_a_moved_database_serves_snapshots_bundles_and_stats(seeded, tmp_path):  # noqa: F811
    from mmw.core.config import NetworkConfig
    from mmw.core.database_move import move_database
    from mmw.core.publisher import publish_network, publish_snapshot
    from mmw.core.publisher.bundle import bundle_network
    from mmw.core.snapshots import take_snapshot
    mirror_dir, cfg = seeded
    net = NetworkConfig(name="lab", root=tmp_path / "pub", bundle_dir=tmp_path / "b", nginx=False)
    cfg = cfg.model_copy(update={"networks": [net]})
    config.save(cfg, tmp_path)
    cfg = config.load(tmp_path)
    publish_network(net, cfg, mirror_dir)
    stamp = take_snapshot(net, cfg, mirror_dir, label="s").stamp

    def seen():
        with Pool(mirror_dir, read_only=True) as pool:
            return (pool.stats()["packages"], pool.unique_stats(["ubuntu-noble-main-amd64"]),
                    len(list(pool.snapshot_blob_rows(pool.snapshots("lab")[0].id))))
    before_move = seen()
    assert before_move[0] > 0
    move_database(tmp_path, cfg, tmp_path / "fast" / "mmw.db")
    assert seen() == before_move
    before = sorted(p.name for p in net.root.rglob("*.deb"))
    assert not publish_snapshot(net, cfg, mirror_dir, stamp).errors
    assert sorted(p.name for p in net.root.rglob("*.deb")) == before
    result = bundle_network(net, cfg, mirror_dir, from_ref=None)
    assert not result.errors and result.sent > 3


def test_wipe_removes_a_moved_database_too(tmp_path):
    from mmw.core.pool import wipe
    md, db = tmp_path / "mirror", tmp_path / "fast" / "mmw.db"
    register_database(md, db)
    try:
        with Pool(md):
            pass
        assert db.exists()
        wipe(md)
        assert not db.exists() and not (md / ".pool").exists()
    finally:
        register_database(md, md / ".pool" / "mmw.db")


from test_bundle import seeded  # noqa: E402,F401
