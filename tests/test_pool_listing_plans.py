"""Listings read the pool database through covering indexes, in checksum order.

On a spinning disk a listing that hops from an index to the table row it names
does one random read per file: a 1M-file mirror took most of an hour. Covering
indexes answer from index pages alone, and walking them in checksum order reads
them front to back.
"""
import hashlib

import pytest

from mmw.core.pool import KIND_META, KIND_PKG, Pool

LEGACY_MIRROR_FILES = (
    "SELECT DISTINCT r.repo_path, p.pool_path, p.size, r.kind, r.checksum "
    "FROM refs r JOIN packages p ON p.checksum = r.checksum "
    "WHERE r.mirror_name IN (?) ORDER BY r.repo_path")


def _add(pool: Pool, content: bytes, name: str) -> str:
    checksum = hashlib.sha256(content).hexdigest()
    pool.add(content, checksum, "sha256", name, len(content))
    return checksum


@pytest.fixture
def seeded(tmp_path):
    md = tmp_path / "mirror"
    with Pool(md, defer_writes=False) as pool:
        refs = []
        for i in range(40):
            cs = _add(pool, f"blob {i}".encode(), f"f{i}.deb")
            refs.append((cs, f"pool/main/f{i}.deb", KIND_PKG if i % 5 else KIND_META))
        refs.append(("f" * 64, "pool/main/missing.deb", KIND_PKG))   # wanted, not held
        pool.reconcile_refs("m", refs)
    return md


def _plan(pool: Pool, sql: str, params) -> str:
    return " | ".join(r[3] for r in pool._conn.execute("EXPLAIN QUERY PLAN " + sql, params))


def test_mirror_files_reads_only_covering_indexes(seeded):
    with Pool(seeded) as pool:
        plan = _plan(pool, *pool._mirror_files_sql(["m"], None))
    assert "COVERING INDEX idx_refs_cover" in plan
    assert "COVERING INDEX idx_packages_cover" in plan


def test_a_prefix_lists_only_its_paths_and_stays_on_covering_indexes(seeded):
    with Pool(seeded) as pool:
        everything = pool.mirror_files(["m"])
        assert pool.mirror_files(["m"], prefix="pool/main/f1") == [
            row for row in everything if row[0].startswith("pool/main/f1")]
        assert pool.mirror_files(["m"], prefix="pool/") == everything
        assert pool.mirror_files(["m"], prefix="nowhere/") == []
        plan = _plan(pool, *pool._mirror_files_sql(["m"], None, "pool/main/f1"))
    assert "COVERING INDEX idx_refs_cover" in plan
    assert "COVERING INDEX idx_packages_cover" in plan


def test_missing_for_reads_only_covering_indexes(seeded):
    with Pool(seeded) as pool:
        plan = _plan(pool, *pool._missing_for_sql("m"))
    assert "COVERING INDEX idx_refs_cover" in plan
    assert "COVERING INDEX" in plan.split("idx_refs_cover", 1)[1]


def test_old_mirror_index_is_replaced(seeded):
    with Pool(seeded) as pool:
        names = {r[0] for r in pool._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert {"idx_refs_cover", "idx_packages_cover"} <= names
    assert "idx_refs_mirror" not in names


def test_listing_rows_are_unchanged(seeded):
    with Pool(seeded) as pool:
        legacy = pool._conn.execute(LEGACY_MIRROR_FILES, ("m",)).fetchall()
        assert pool.mirror_files(["m"]) == legacy
        assert [r[0] for r in pool.missing_for("m")] == ["pool/main/missing.deb"]


def test_listing_derives_the_blob_path_when_the_stored_one_is_stale(seeded):
    with Pool(seeded) as pool:
        pool._conn.execute("UPDATE packages SET pool_path = '/gone/elsewhere'")
        pool._conn.commit()
        rows = pool.mirror_files(["m"])
    assert rows and all("/gone/" not in r[1] for r in rows)
    assert all(r[1].startswith(str(seeded / ".pool" / "packages")) for r in rows)


def test_an_old_database_opened_read_only_still_lists(seeded):
    with Pool(seeded) as pool:
        expected = pool.mirror_files(["m"])
        missing = pool.missing_for("m")
        pool._conn.execute("DROP INDEX idx_refs_cover")
        pool._conn.execute("DROP INDEX idx_packages_cover")
        pool._conn.execute("CREATE INDEX idx_refs_mirror ON refs(mirror_name, checksum)")
        pool._conn.commit()
    with Pool(seeded, read_only=True) as pool:
        assert pool.mirror_files(["m"]) == expected
        assert pool.missing_for("m") == missing


def test_snapshot_rows_and_lookups_derive_the_blob_path(seeded):
    with Pool(seeded) as pool:
        rows = [(rel, cs) for rel, _p, _s, _k, cs in pool.mirror_files(["m"])]
        snap = pool.create_snapshot("lab", "20261016T000000Z", kind="manual", rows=iter(rows))
        pool._conn.execute("UPDATE packages SET pool_path = '/gone/elsewhere'")
        pool._conn.commit()
        held = [r for r in pool.snapshot_blob_rows(snap) if r[2] is not None]
        assert held and all(str(r[2]).startswith(str(seeded / ".pool")) for r in held)
        assert str(pool.resolve("m", rows[0][0])).startswith(str(seeded / ".pool"))
        assert str(pool.find_blob(rows[0][0])).startswith(str(seeded / ".pool"))


def test_listings_name_each_blob_exactly_as_blob_path_does(seeded):
    with Pool(seeded) as pool:
        rows = pool.mirror_files(["m"])
        assert rows and all(isinstance(path, str) for _, path, *_ in rows)
        by_checksum = dict(pool._conn.execute("SELECT checksum, filename FROM packages"))
        assert [path for _, path, _, _, _ in rows] == [
            str(pool.blob_path(cs, by_checksum[cs])) for *_, cs in rows]


def test_the_publish_count_is_one_per_path_the_pool_can_place(tmp_path):
    md = tmp_path / "mirror"
    with Pool(md, defer_writes=False) as pool:
        icon = _add(pool, b"same icon", "icon.png")
        pool.reconcile_refs("m", [
            (icon, "p.gallerycdn.vsassets.io/x/1.0/icon.png", KIND_PKG),
            (icon, "p.gallerycdn.vsassets.io/x/2.0/icon.png", KIND_PKG),   # same bytes
            ("f" * 64, "p.gallerycdn.vsassets.io/x/2.0/VSIX", KIND_PKG),   # not held
            (_add(pool, b"{}", "x.json"), ".vscode-meta/extensions/universal/p.x.json", KIND_META),
        ])
        assert pool.group_unique_count(["m"]) == 2
        assert len(pool.mirror_files(["m"], kinds=(KIND_PKG,))) == 2   # what publish places
