"""The pool's `mirrors` table records unit coordinates as JSON `dims`, and
still reads pools written before the column existed."""
import hashlib
import json
import sqlite3

from mmw.core.config import MirrorConfig
from mmw.core.pool import KIND_PKG, Pool

SRC = "https://archive.ubuntu.com/ubuntu"


def _unit(**dims) -> MirrorConfig:
    return MirrorConfig(name="u-noble-main-arm64", provider="apt", source=SRC,
                        source_url=SRC, label="ubuntu", dist_upgrade=True, **dims)


def _seed(pool: Pool, unit: MirrorConfig) -> None:
    cs = hashlib.sha256(b"x").hexdigest()
    pool.add(b"x", cs, "sha256", "x.deb", 1)
    pool.reconcile_refs(unit.name, [(cs, "pool/x.deb", KIND_PKG)], source=SRC)
    pool.record_mirror(unit)


def test_dims_round_trip(tmp_path):
    with Pool(tmp_path) as pool:
        _seed(pool, _unit(suite="noble", component="main", arch="arm64"))
    with Pool(tmp_path, read_only=True) as pool:
        (m,) = pool.mirrors([SRC])
    assert m.dims == {"suite": "noble", "component": "main", "arch": "arm64"}
    assert m.dist_upgrade is True and m.label == "ubuntu"


def _make_legacy_pool(tmp_path) -> None:
    """A pool as written before `dims`: create, then drop the column."""
    with Pool(tmp_path) as pool:
        _seed(pool, _unit(suite="noble", component="main", arch="arm64"))
    db = sqlite3.connect(tmp_path / ".pool" / "mmw.db")
    db.execute("ALTER TABLE mirrors DROP COLUMN dims")
    db.commit()
    db.close()


def test_read_only_open_of_a_legacy_pool_uses_legacy_columns(tmp_path):
    _make_legacy_pool(tmp_path)
    with Pool(tmp_path, read_only=True) as pool:
        (m,) = pool.mirrors([SRC])
    assert (m.suite, m.component, m.arch) == ("noble", "main", "arm64")


def test_writable_open_migrates_legacy_rows(tmp_path):
    _make_legacy_pool(tmp_path)
    Pool(tmp_path).close()
    db = sqlite3.connect(tmp_path / ".pool" / "mmw.db")
    (dims,) = db.execute("SELECT dims FROM mirrors").fetchone()
    db.close()
    assert json.loads(dims) == {"arch": "arm64", "component": "main", "suite": "noble"}


def test_legacy_columns_are_still_written(tmp_path):
    with Pool(tmp_path) as pool:
        _seed(pool, _unit(suite="noble", component="main", arch="arm64"))
    db = sqlite3.connect(tmp_path / ".pool" / "mmw.db")
    row = db.execute("SELECT arch, suite, component FROM mirrors").fetchone()
    db.close()
    assert row == ("arm64", "noble", "main")
