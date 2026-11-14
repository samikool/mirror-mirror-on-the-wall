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


def _pypi_universal() -> MirrorConfig:
    """PyPI's pure-wheels/sdists unit: it has no coordinates at all."""
    return MirrorConfig(name="pypi-all-universal", provider="pypi", source="https://pypi.org/simple",
                        source_url="https://pypi.org/simple", options={"targets": ["cp312-linux-x86_64"]})


def _seed_any(pool: Pool, unit: MirrorConfig) -> None:
    cs = hashlib.sha256(b"x").hexdigest()
    pool.add(b"x", cs, "sha256", "x.whl", 1)
    pool.reconcile_refs(unit.name, [(cs, "x.whl", KIND_PKG)], source=unit.source)
    pool.record_mirror(unit)


def test_a_unit_with_no_dims_reads_back_with_none(tmp_path):
    """`{}` is a real answer for a non-apt unit, not a pre-dims row: reading
    it as legacy gave PyPI's universal unit `{"arch": ""}`, and its label
    then raised KeyError: 'python' when the mirror was selected."""
    from mmw.providers.pypi.provider import PypiProvider
    with Pool(tmp_path) as pool:
        _seed_any(pool, _pypi_universal())
    Pool(tmp_path).close()                      # a writable open must not "repair" it either
    with Pool(tmp_path, read_only=True) as pool:
        (m,) = pool.mirrors()
    assert m.dims == {}
    assert PypiProvider().unit_label(m)[0].endswith("pure wheels · sdists")


def test_a_no_dims_unit_an_earlier_mmw_already_corrupted_is_repaired(tmp_path):
    """The earlier misread was also written back by every writable open, so
    pools in the field hold `{"arch": ""}` for the unit — read it as `{}`, and
    let a writable open put `{}` back."""
    with Pool(tmp_path) as pool:
        _seed_any(pool, _pypi_universal())
    db = sqlite3.connect(tmp_path / ".pool" / "mmw.db")
    db.execute("""UPDATE mirrors SET dims = '{"arch": ""}'""")
    db.commit()
    db.close()
    with Pool(tmp_path, read_only=True) as pool:
        (m,) = pool.mirrors()
    assert m.dims == {}
    Pool(tmp_path).close()
    db = sqlite3.connect(tmp_path / ".pool" / "mmw.db")
    (dims,) = db.execute("SELECT dims FROM mirrors").fetchone()
    db.close()
    assert json.loads(dims) == {}
