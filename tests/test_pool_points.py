"""Timeline points in the connected side's pool: one per (track, Release,
file set), linked to the networks that saw them; a held point's files stay."""
import hashlib
import sqlite3

from mmw.core.pool import KIND_PKG, Pool
from mmw.site.tracks import PointDraft


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _draft(ident="release:r1", date="20261101T000000Z", files=None, track="h/debian/dists/bookworm"):
    return PointDraft(track, ident, date, {"Suite": "bookworm"}, files or {f"{track}/Release": "c1"})


def test_record_point_dedupes_by_ident_and_files(tmp_path):
    with Pool(tmp_path) as pool:
        pid, new = pool.record_point("R", _draft(), "20261101T120000Z")
        assert new and pool.record_point("R", _draft(), "20261102T120000Z") == (pid, False)
        wider = _draft(files={"h/debian/dists/bookworm/Release": "c1", "h/debian/pool/a.deb": "c2"})
        other, new2 = pool.record_point("R", wider, "20261102T120000Z")
        assert new2 and other != pid                       # same Release, another selection
        assert pool.point_files(other) == {"h/debian/dists/bookworm/Release": "c1", "h/debian/pool/a.deb": "c2"}


def test_network_points_order_and_links(tmp_path):
    with Pool(tmp_path) as pool:
        p1, _ = pool.record_point("R", _draft("release:a", "20261101T000000Z"), "s1")
        p2, _ = pool.record_point("R", _draft("release:b", "20261103T000000Z"), "s2")
        p3, _ = pool.record_point("R", _draft("release:c", "20261102T000000Z"), "s3")
        for p in (p1, p2, p3):
            assert pool.link_network_point("oak", p, "20261115T000000Z")
        assert not pool.link_network_point("oak", p1, "20261116T000000Z")    # already linked
        pool.link_network_point("elm", p1, "20261115T000000Z")
        (track,) = pool.network_tracks("oak")
        assert track.repo == "R" and track.suite == "bookworm"
        assert [p.ident for p in pool.network_points("oak", track.id)] == ["release:b", "release:c", "release:a"]
        assert [p.ident for p in pool.network_points("elm")] == ["release:a"]


def test_a_held_points_blob_is_not_an_orphan(tmp_path):
    with Pool(tmp_path) as pool:
        data = b"old package"
        cs = _sha(data)
        pool.add(data, cs, "sha256", "old.deb", len(data))
        pool.reconcile_refs("m", [], source="S")             # nothing refers to it any more
        pid, _ = pool.record_point("R", _draft(files={"h/debian/pool/old.deb": cs}), "s")
        assert pool.prune_preview(["S"]) == (0, 0)
        pool.stub_point(pid)
        assert pool.prune_preview(["S"])[0] == 1


def test_snapshot_points_link(tmp_path):
    with Pool(tmp_path) as pool:
        pid, _ = pool.record_point("R", _draft(), "s")
        sid = pool.create_snapshot("oak", "20261101T000000Z", kind="manual", rows=[])
        pool.link_snapshot_points(sid, [pid])
        pool.link_snapshot_points(sid, [pid])                # idempotent
        assert pool.snapshot_point_ids(sid) == [pid]


def test_old_pool_gains_tables(tmp_path):
    Pool(tmp_path).close()
    db = sqlite3.connect(tmp_path / ".pool" / "mmw.db")
    for t in ("network_points", "snapshot_points", "point_files", "points", "tracks"):
        db.execute(f"DROP TABLE {t}")
    db.commit()
    db.close()
    with Pool(tmp_path) as pool:
        assert pool.network_points("oak") == []


KIND_PKG  # noqa: B018 — imported for parity with other pool tests
