"""Managing snapshots: finding one by label or stamp, the cleaner (keep the
newest N), and importing the bundle history that predates snapshots."""
import pytest

from mmw.core import snapshots as S
from mmw.core.pool import Pool
from mmw.core.publisher.manifest import ManifestHeader, ledger_dir, write_manifest

from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture


def test_finding_a_snapshot_by_stamp_label_or_id(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    a = S.take_snapshot(net, cfg, mirror_dir, label="golden")
    b = S.take_snapshot(net, cfg, mirror_dir)
    with Pool(mirror_dir) as pool:
        assert S.find_snapshot(pool, "oak", "golden").id == a.id
        assert S.find_snapshot(pool, "oak", b.stamp).id == b.id
        assert S.find_snapshot(pool, "oak", str(a.id)).id == a.id
        with pytest.raises(LookupError, match="no snapshot"):
            S.find_snapshot(pool, "oak", "nope")
        pool.rename_snapshot(b.id, "golden")
        with pytest.raises(LookupError, match="2 snapshots"):
            S.find_snapshot(pool, "oak", "golden")


def test_the_cleaner_keeps_the_newest_n_and_says_what_it_frees(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    taken = [S.take_snapshot(net, cfg, mirror_dir) for _ in range(4)]
    with Pool(mirror_dir) as pool:
        plan = S.clean_plan(pool, "oak", keep=2)
        assert [s.id for s in plan.delete] == [taken[1].id, taken[0].id]   # oldest two
        assert plan.frees == 0      # every file is still live — nothing to free
        S.delete_snapshots(pool, plan.delete)
        assert [s.id for s in pool.snapshots("oak")] == [taken[3].id, taken[2].id]
        assert S.clean_plan(pool, "oak", keep=5).delete == []


def test_bundle_history_is_imported_as_incomplete_snapshots_once(two_mirrors):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    d = ledger_dir(mirror_dir, "oak")
    for stamp, prev in (("20260901T000000Z", None), ("20260902T000000Z", "20260901T000000Z")):
        write_manifest(d / f"{stamp}.manifest.jsonl.zst",
                       ManifestHeader(network="oak", created=stamp, previous=prev, full=False),
                       [("a/pool/x.deb", "c1"), ("nginx/mirror.conf", "c9")])
    with Pool(mirror_dir) as pool:
        assert S.import_ledger(pool, mirror_dir, "oak") == 2
        assert S.import_ledger(pool, mirror_dir, "oak") == 0          # idempotent
        snaps = pool.snapshots("oak")
        assert [(s.stamp, s.kind, s.complete) for s in snaps] == [
            ("20260902T000000Z", "imported", False), ("20260901T000000Z", "imported", False)]
        assert dict(pool.snapshot_files(snaps[0].id)) == {"a/pool/x.deb": "c1"}


def test_deleting_a_transferred_snapshot_leaves_it_history_only(two_mirrors):  # noqa: F811
    # The far side may sit at it: its list stays as a starting point, and
    # the ledger manifest under its stamp must not bring it back whole.
    cfg, net, mirror_dir = two_mirrors
    sent = S.take_snapshot(net, cfg, mirror_dir)
    kept = S.take_snapshot(net, cfg, mirror_dir)
    write_manifest(ledger_dir(mirror_dir, "oak") / f"{sent.stamp}.manifest.jsonl",
                   ManifestHeader(network="oak", created=sent.stamp, previous=None, full=True),
                   [("a/pool/x.deb", "c1")])
    with Pool(mirror_dir) as pool:
        S.ensure_repo_versions(pool, "oak")
        versions = pool.snapshot_repo_versions(sent.id)
        pool.record_bundle("oak", None, sent.stamp, "/o/x.tar", 1, 1, 0)
        S.delete_snapshot(pool, sent)
        S.delete_snapshot(pool, kept)                   # never transferred: really gone
        assert S.import_ledger(pool, mirror_dir, "oak") == 0
        [snap] = pool.snapshots("oak")
        assert (snap.id, snap.complete) == (sent.id, False)
        assert pool.snapshot_repo_versions(sent.id) == versions
        with pytest.raises(ValueError, match="history only"):
            S.delete_snapshot(pool, snap)
