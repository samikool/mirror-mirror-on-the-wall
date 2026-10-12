"""The provider seam's compatibility edges: units and selects behave like the
fields they replaced, and a pool shared with an older mmw stays truthful."""
import hashlib
import json
import sqlite3

import pytest
from pydantic import ValidationError

from mmw.core.config import MirrorConfig, NetworkSelect
from mmw.core.pool import KIND_PKG, Pool

SRC = "https://archive.ubuntu.com/ubuntu"


def _unit(**kw) -> MirrorConfig:
    return MirrorConfig(name="u", provider="apt", source=SRC, source_url=SRC, **kw)


# ── units behave like the old flat fields ────────────────────────────────────

def test_a_shallow_copy_does_not_share_coordinates_or_options():
    m = _unit(arch="amd64", extra_files=["https://x/a"])
    c = m.model_copy()
    c.arch = "arm64"
    c.extra_files.append("https://x/b")
    assert m.arch == "amd64"
    assert m.extra_files == ["https://x/a"]


def test_model_copy_update_accepts_the_old_field_names():
    c = _unit(arch="amd64", suite="noble").model_copy(
        update={"arch": "arm64", "suite": None, "gpg_key": "K", "label": "ubuntu"})
    assert (c.arch, c.suite, c.gpg_key, c.label) == ("arm64", None, "K", "ubuntu")


def test_extra_files_can_be_appended_in_place():
    m = _unit()
    m.extra_files.append("https://x/a")
    assert m.extra_files == ["https://x/a"]


def test_equality_ignores_spelled_out_defaults():
    assert _unit(include_sources=False, gpg_key=None, extra_files=[]) == _unit()
    assert _unit(include_sources=True) != _unit()
    assert _unit(arch="arm64") != _unit()


# ── a pool an older mmw also writes to ───────────────────────────────────────

def _seed(pool: Pool, unit: MirrorConfig) -> None:
    cs = hashlib.sha256(b"x").hexdigest()
    pool.add(b"x", cs, "sha256", "x.deb", 1)
    pool.reconcile_refs(unit.name, [(cs, "x.deb", KIND_PKG)], source=SRC)
    pool.record_mirror(unit)


def _old_mmw_rerecords_arch(tmp_path, arch: str) -> None:
    """What an older mmw's record_mirror does: update the legacy columns only."""
    db = sqlite3.connect(tmp_path / ".pool" / "mmw.db")
    db.execute("UPDATE mirrors SET arch = ? WHERE name = 'u'", (arch,))
    db.commit()
    db.close()


def test_legacy_columns_rewritten_by_an_older_mmw_win_on_read(tmp_path):
    with Pool(tmp_path) as pool:
        _seed(pool, _unit(arch="amd64"))
    _old_mmw_rerecords_arch(tmp_path, "arm64")
    with Pool(tmp_path, read_only=True) as pool:
        (m,) = pool.mirrors([SRC])
    assert m.arch == "arm64"


def test_writable_open_repairs_dims_an_older_mmw_left_stale(tmp_path):
    with Pool(tmp_path) as pool:
        _seed(pool, _unit(arch="amd64", suite="noble"))
    _old_mmw_rerecords_arch(tmp_path, "arm64")
    Pool(tmp_path).close()
    db = sqlite3.connect(tmp_path / ".pool" / "mmw.db")
    (dims,) = db.execute("SELECT dims FROM mirrors").fetchone()
    db.close()
    assert json.loads(dims) == {"arch": "arm64", "suite": "noble"}


def test_legacy_columns_never_add_a_coordinate_the_unit_lacks(tmp_path):
    """Only keys the stored dims already has are refreshed — a future
    provider's unit with no arch must not gain `arch: ""` from an empty column."""
    with Pool(tmp_path) as pool:
        _seed(pool, _unit(arch="amd64"))
    db = sqlite3.connect(tmp_path / ".pool" / "mmw.db")
    db.execute("""UPDATE mirrors SET dims = '{"arch": "amd64"}', suite = 'noble'""")
    db.commit()
    db.close()
    with Pool(tmp_path, read_only=True) as pool:
        (m,) = pool.mirrors([SRC])
    assert m.dims == {"arch": "amd64"}


# ── network selects read from a file ─────────────────────────────────────────

def test_a_literal_dims_table_is_not_accepted():
    with pytest.raises(ValidationError, match="flat keys"):
        NetworkSelect.model_validate({"source": SRC, "dims": {"suites": ["noble"]}})


def test_a_select_without_a_source_says_it_is_required():
    with pytest.raises(ValidationError, match="Field required"):
        NetworkSelect.model_validate({"suites": ["noble"]})
