"""Every bundle records, per repository, which version it sent — and
cleaning keeps the snapshot it went to as history only, versions and all."""
from mmw.core.pool import Pool
from mmw.core.publisher.bundle import bundle_network
from mmw.core.snapshots import clean_plan, delete_snapshots, repo_version_files, take_snapshot

from test_dry_run_totals import two_mirrors  # noqa: F401 — the shared pool fixture
from test_snapshot_repo_versions import _change_a, _key


def test_apply_bundle_records_every_repo(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    bundle_network(net, cfg, mirror_dir, out_path=tmp_path / "out", from_ref=None)
    with Pool(mirror_dir) as p:
        dest = p.snapshots(net.name)[0]
        assert p.last_sent_versions(net.name) == {k: v.id for k, v in p.snapshot_repo_versions(dest.id).items()}
        first = p.bundles(net.name)[0]
    _change_a(mirror_dir)
    bundle_network(net, cfg, mirror_dir, out_path=tmp_path / "out")
    with Pool(mirror_dir) as p:
        dest2 = p.snapshots(net.name)[0]
        sent = p.last_sent_versions(net.name)
        v2 = p.snapshot_repo_versions(dest2.id)
        assert sent == {k: v.id for k, v in v2.items()}
        assert v2[_key(cfg, "a")].number == 2 and v2[_key(cfg, "b")].number == 1
        rows = dict(((r, (a, b)) for r, a, b in p._conn.execute(
            "SELECT repo, from_version, to_version FROM bundle_repos WHERE bundle = ?",
            (p.bundles(net.name)[0].id,))))
        assert rows[_key(cfg, "a")][0] is not None                      # a delta, from v1
        assert first.kind == "bundle"


def test_clean_leaves_the_sent_snapshot_history_only_with_its_versions(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, mirror_dir = two_mirrors
    bundle_network(net, cfg, mirror_dir, out_path=tmp_path / "out", from_ref=None)    # sends v1 of both
    _change_a(mirror_dir)
    take_snapshot(net, cfg, mirror_dir)
    take_snapshot(net, cfg, mirror_dir)
    with Pool(mirror_dir) as p:
        sent_snap, unsent = p.snapshots(net.name)[-1], p.snapshots(net.name)[1]
        plan = clean_plan(p, net.name, keep=1)
        assert {s.stamp for s in plan.delete} == {sent_snap.stamp, unsent.stamp}
        assert plan.history == {sent_snap.stamp}
        delete_snapshots(p, plan.delete)
        assert [(s.stamp, s.complete) for s in p.snapshots(net.name)][1:] == [(sent_snap.stamp, False)]
        # The ledger's versions are still there for the next delta to start from.
        for v in p.last_sent_versions(net.name).values():
            assert repo_version_files(p, p.repo_version(v)) is not None
