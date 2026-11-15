"""Force delete: forgetting a snapshot outright — its rows, the transfers
that went to it and its ledger manifest. Like a force push, the far side may
be left where nothing here records, so its next default transfer is full —
unless it runs mmw, whose bundles remember what was sent."""
import pytest

from mmw.core import snapshots as S
from mmw.core.pool import Pool
from mmw.core.publisher.bundle import bundle_network
from mmw.core.publisher.manifest import ledger_dir

from test_bundle_mmw import _mmw
from test_dry_run_totals import SHARED, _index, _sha, two_mirrors  # noqa: F401 — the shared pool fixture
from test_snapshot_repo_versions import _change_a


def _change_a_again(mirror_dir):
    """Mirror `a` gains another package."""
    from mmw.core.pool import KIND_META, KIND_PKG
    files = {"shared.deb": SHARED, "a.deb": b"a" * 300, "a2.deb": b"A" * 50, "a3.deb": b"3" * 70}
    with Pool(mirror_dir) as pool:
        refs = []
        for fname, data in files.items():
            pool.add(data, _sha(data), "sha256", fname, len(data))
            refs.append((_sha(data), fname, KIND_PKG))
        idx = _index(files)
        pool.add(idx, _sha(idx), "sha256", "Packages", len(idx))
        refs.append((_sha(idx), "Packages", KIND_META))
        pool.reconcile_refs("a", refs)


def _ledger(mirror_dir, network):
    d = ledger_dir(mirror_dir, network)
    return sorted(p.name.split(".")[0] for p in d.iterdir()) if d.is_dir() else []


def test_forgetting_where_the_far_side_is_makes_the_next_transfer_full(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = two_mirrors
    first = bundle_network(net, cfg, md, out_path=tmp_path / "1", from_ref=None)
    _change_a(md)
    second = bundle_network(net, cfg, md, out_path=tmp_path / "2")
    assert second.previous == first.created
    with Pool(md) as p:
        snap = S.find_snapshot(p, net.name, second.created)
        warned = " ".join(S.forget_warnings(p, snap))
        assert "sends everything" in warned and "never deleted over there" in warned
        assert S.forget_snapshot(p, md, snap)
        assert [b.to_stamp for b in p.bundles(net.name)] == [first.created]
        assert p.full_next(net.name) == second.created
        assert S.import_ledger(p, md, net.name) == 0              # its manifest went too
        assert [s.stamp for s in p.snapshots(net.name)] == [first.created]
    assert _ledger(md, net.name) == [first.created]

    third = bundle_network(net, cfg, md, out_path=tmp_path / "3")
    assert third.full and third.previous is None
    assert any(second.created in w and "force-deleted" in w for w in third.warnings)
    with Pool(md) as p:
        assert p.full_next(net.name) is None                       # a full one settles it
    _change_a_again(md)
    assert not bundle_network(net, cfg, md, out_path=tmp_path / "4").full


def test_forgetting_an_older_transfer_leaves_the_next_one_a_delta(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = two_mirrors
    first = bundle_network(net, cfg, md, out_path=tmp_path / "1", from_ref=None)
    _change_a(md)
    second = bundle_network(net, cfg, md, out_path=tmp_path / "2")
    with Pool(md) as p:
        snap = S.find_snapshot(p, net.name, first.created)
        assert "sends everything" not in " ".join(S.forget_warnings(p, snap))
        assert not S.forget_snapshot(p, md, snap)
        assert p.full_next(net.name) is None
    _change_a_again(md)
    assert bundle_network(net, cfg, md, out_path=tmp_path / "3").previous == second.created


def test_forgetting_a_snapshot_needs_no_full_bundle(two_mirrors, tmp_path):  # noqa: F811
    # An mmw network's bundles carry points and remember what was sent:
    # forgetting a snapshot rewrites no record of what the far side has.
    cfg, net, md = _mmw(two_mirrors)
    snap = S.take_snapshot(net, cfg, md, kind="manual")
    bundle_network(net, cfg, md, out_path=tmp_path / "1", to_ref=snap.stamp)
    with Pool(md) as p:
        snap = S.find_snapshot(p, net.name, snap.stamp)
        warned = " ".join(S.forget_warnings(p, snap, "mmw"))
        assert "sends everything" not in warned and "never deleted" not in warned
        assert not S.forget_snapshot(p, md, snap, "mmw")
        assert p.full_next(net.name) is None
    _change_a(md)
    r = bundle_network(net, cfg, md, out_path=tmp_path / "2")
    assert not r.full


def test_forgetting_a_format2_snapshot_keeps_what_it_sent(two_mirrors, tmp_path):  # noqa: F811
    # A format-2 bundle went to it before bundles carried points: what it
    # sent is recorded before the snapshot goes, so the next bundle is a delta.
    from mmw.core.publisher.manifest import blob_member
    from test_bundle_format3 import _open
    cfg, net, md = _mmw(two_mirrors)
    snap = S.take_snapshot(net, cfg, md, kind="bundle")
    with Pool(md) as p:
        versions = p.snapshot_repo_versions(snap.id)
        p.record_bundle(net.name, None, snap.stamp, "old.tar", 0, 0, 0,
                        repos={repo: (None, v.id) for repo, v in versions.items()})
        assert not S.forget_snapshot(p, md, S.find_snapshot(p, net.name, snap.stamp), "mmw")
        assert p.full_next(net.name) is None
    _change_a(md)
    names, _h, rows = _open(bundle_network(net, cfg, md, out_path=tmp_path / "2.tar").out_path)
    shared = next(r["c"] for r in rows if r["p"].endswith("/shared.deb"))
    assert blob_member(shared) not in names


def test_a_served_snapshot_is_never_forgotten_and_names_follow_the_network(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = two_mirrors
    sent = bundle_network(net, cfg, md, out_path=tmp_path / "1", from_ref=None)
    with Pool(md) as p:
        snap = S.find_snapshot(p, net.name, sent.created)
        p.record_published("/srv/www", net.name, snap.stamp)
        with pytest.raises(ValueError, match="published at"):
            S.forget_snapshot(p, md, snap)
        p.record_published("/srv/www", net.name, "live")
        assert S.forget_snapshot(p, md, snap)
        assert p.rename_network(net.name, "elm") is None
        assert p.full_next("elm") == sent.created
        p.forget_network("elm")
        assert p.full_next("elm") is None


def test_cli_force_delete_asks_or_needs_yes(two_mirrors, tmp_path, monkeypatch):  # noqa: F811
    from mmw.core import config
    from test_snapshot_publish import _mmw as run
    cfg, net, md = two_mirrors
    sent = bundle_network(net, cfg, md, out_path=tmp_path / "1", from_ref=None)
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    config.save(cfg, cfg_dir)
    r = run(monkeypatch, cfg_dir, "snapshot", "delete", "-n", net.name, sent.created, "--force")
    assert r.exit_code == 1 and "rewrites history" in r.output and "--yes" in r.output
    r = run(monkeypatch, cfg_dir, "snapshot", "delete", "-n", net.name, sent.created, "--force", "--yes")
    assert r.exit_code == 0 and "next transfer sends everything" in r.output, r.output
    with Pool(md) as p:
        assert p.snapshots(net.name) == [] and p.full_next(net.name) == sent.created
