"""A snapshot taken before mmw ran on both sides lists apply.py (and with
certs sign.py, make-ca.py): replaying it never lays them out again, and the
tree's record names only what is there."""
import hashlib

from mmw.core import snapshots as S
from mmw.core.pool import Pool
from mmw.core.publisher import publish_snapshot
from mmw.core.publisher.manifest import RECORD_PATH, iter_manifest

from test_dry_run_totals import two_mirrors  # noqa: F401


def test_an_old_snapshots_scripts_are_not_laid_out(two_mirrors, tmp_path):  # noqa: F811
    cfg, net, md = two_mirrors
    snap = S.take_snapshot(net, cfg, md, kind="manual")
    old = b"#!/usr/bin/env python3\n# apply.py\n"
    cs = hashlib.sha256(old).hexdigest()
    with Pool(md) as pool:                               # as a snapshot taken by an older mmw
        pool.add(old, cs, "sha256", "apply.py", len(old))
        rows = dict(pool.snapshot_files(snap.id))
        rows["apply.py"] = cs
        pool.delete_snapshot(snap.id)
        pool.create_snapshot(net.name, snap.stamp, kind="manual", rows=sorted(rows.items()), settings=snap.settings)
    root = tmp_path / "replay"
    assert not publish_snapshot(net, cfg, md, snap.stamp, root=root).errors
    assert not (root / "apply.py").exists()
    _h, recorded = iter_manifest(root / RECORD_PATH)
    assert "apply.py" not in dict(recorded)
