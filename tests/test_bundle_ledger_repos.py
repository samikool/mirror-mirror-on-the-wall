"""Every bundle records, per repository, which version it sent — and
cleaning keeps the snapshot it went to as history only, versions and all."""
from mmw.core.pool import Pool
from mmw.core.publisher.bundle import bundle_network
from mmw.core.snapshots import clean_plan, delete_snapshots, take_snapshot

from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture
from test_snapshot_repo_versions import _change_a


def test_clean_leaves_the_sent_snapshot_history_only_with_its_versions(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    # A transferred snapshot is an explicit label, not a side effect of a
    # bundle. Relying on the first manual snapshot sharing the bundle's
    # wall-clock second made this assertion depend on machine timing.
    first = take_snapshot(net, cfg, mirror_dir)
    bundle_network(net, cfg, mirror_dir, out_path=tmp_path / "out", from_ref=None, to_ref=first.stamp)
    _change_a(mirror_dir)
    take_snapshot(net, cfg, mirror_dir)
    take_snapshot(net, cfg, mirror_dir)
    with Pool(mirror_dir) as p:
        sent_snap, unsent = p.snapshots(net.name)[-1], p.snapshots(net.name)[1]
        versions = p.snapshot_repo_versions(sent_snap.id)
        assert versions
        plan = clean_plan(p, net.name, keep=1)
        assert {s.stamp for s in plan.delete} == {sent_snap.stamp, unsent.stamp}
        assert plan.history == {sent_snap.stamp}
        delete_snapshots(p, plan.delete)
        assert [(s.stamp, s.complete) for s in p.snapshots(net.name)][1:] == [(sent_snap.stamp, False)]
        # Demotion preserves the snapshot's version metadata; history-only
        # labels stop holding blobs, while sent records remain independent.
        assert p.snapshot_repo_versions(sent_snap.id) == versions
