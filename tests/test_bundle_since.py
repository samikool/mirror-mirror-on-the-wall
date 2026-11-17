"""Explicit far-side baselines replace the connected side's sent record."""
import pytest

from mmw.core import points, snapshots
from mmw.core.pool import Pool
from mmw.core.publisher.bundle import NothingToStandOn, bundle_network
from mmw.core.publisher.manifest import blob_member

from test_bundle_format3 import _a_dates, _open, mmw_net  # noqa: F401
from test_site_apply_timeline import set_a
from test_dry_run_totals import two_mirrors  # noqa: F401


def bundle(env, out, **kwargs):
    cfg, net, md = env
    return bundle_network(net, cfg, md, out_path=out, **kwargs)


def clock(monkeypatch, day):
    monkeypatch.setattr(snapshots, "now_stamp", lambda: f"202611{day:02}T120000Z")


def test_since_a_kept_manifest_sends_only_what_it_lacks(mmw_net, tmp_path, monkeypatch):  # noqa: F811
    clock(monkeypatch, 1)
    first = bundle(mmw_net, tmp_path / "kept")
    cfg, net, md = mmw_net
    # Lose all local sent state; only a kept bundle remains as evidence.
    with Pool(md) as pool:
        pool._conn.execute("DELETE FROM sent_blobs")
        pool._conn.execute("DELETE FROM sent_points")
        pool._conn.commit()
    set_a(md, 2)
    clock(monkeypatch, 2)
    result = bundle(mmw_net, tmp_path / "second.tar", since="2026-11-01", kept_dirs=[first.out_path.parent])
    assert result.since_source == f"{first.out_path.name} (its manifest)"
    names, _, rows = _open(result.out_path)
    old = {row["c"] for row in _open(first.out_path)[2]}
    assert all(blob_member(row["c"]) not in names for row in rows if row["c"] in old)
    assert result.sent == 3  # the new package, index and Release; nginx unchanged


def test_since_own_points_when_no_manifest(mmw_net, tmp_path, monkeypatch):  # noqa: F811
    cfg, net, md = mmw_net
    points.record_network_points(net, cfg, md, stamp="20261101T120000Z")
    set_a(md, 2)
    clock(monkeypatch, 2)
    result = bundle(mmw_net, tmp_path / "delta.tar", since="2026-11-01")
    assert result.since_source == "the points of 2026-11-01"
    names, _, rows = _open(result.out_path)
    shared = next(row["c"] for row in rows if row["p"].endswith("/shared.deb"))
    assert blob_member(shared) not in names


def test_since_nothing_found_offers_full(mmw_net, tmp_path):  # noqa: F811
    with pytest.raises(NothingToStandOn, match="searched.*--full"):
        bundle(mmw_net, tmp_path / "delta.tar", since="2000-01-01")


def test_since_ignores_the_sent_record_and_recovers_history(mmw_net, tmp_path, monkeypatch):  # noqa: F811
    cfg, net, md = mmw_net
    clock(monkeypatch, 1)
    bundle(mmw_net, tmp_path / "kept")
    set_a(md, 2)
    clock(monkeypatch, 2)
    bundle(mmw_net, tmp_path / "lost.tar")
    set_a(md, 3)
    clock(monkeypatch, 3)
    result = bundle(mmw_net, tmp_path / "recovery.tar", since="2026-11-01", history=True)
    names, header, rows = _open(result.out_path)
    assert _a_dates(header, cfg) == ["20261102", "20261103"]
    assert any(row["p"].endswith("/a2.deb") and blob_member(row["c"]) in names for row in rows)


def test_next_plain_bundle_after_since_is_a_delta(mmw_net, tmp_path, monkeypatch):  # noqa: F811
    cfg, net, md = mmw_net
    points.record_network_points(net, cfg, md, stamp="20261101T120000Z")
    set_a(md, 2)
    clock(monkeypatch, 2)
    bundle(mmw_net, tmp_path / "delta.tar", since="2026-11-01")
    set_a(md, 3)
    clock(monkeypatch, 3)
    result = bundle(mmw_net, tmp_path / "next.tar")
    assert result.sent == 3


def test_since_future_uses_the_newest_manifest(mmw_net, tmp_path, monkeypatch):  # noqa: F811
    clock(monkeypatch, 1)
    first = bundle(mmw_net, tmp_path / "kept")
    set_a(mmw_net[2], 2)
    clock(monkeypatch, 2)
    result = bundle(mmw_net, tmp_path / "next.tar", since="2099-01-01")
    assert result.since_source == f"{first.manifest_path.name} (its manifest)"
    assert result.sent == 3


def test_rebuilt_pool_from_kept_bundle_applies_like_an_unbroken_chain(mmw_net, tmp_path, monkeypatch):  # noqa: F811
    from pathlib import Path
    from mmw.site import home as homes
    from mmw.site.apply import apply_bundle
    cfg, net, md = mmw_net
    cfg = cfg.model_copy(update={"sources": [cfg.sources[0]]})
    mmw_net = cfg, net, md
    clock(monkeypatch, 1)
    first = bundle(mmw_net, tmp_path / "kept")
    set_a(md, 2)
    clock(monkeypatch, 2)
    ordinary = bundle(mmw_net, tmp_path / "ordinary.tar")
    # A new pool has neither points nor sent records. Re-sync its units.
    rebuilt = tmp_path / "rebuilt"
    with Pool(md, read_only=True) as old, Pool(rebuilt) as fresh:
        for unit in ("a", "b"):
            refs = []
            for rel, path, size, kind, cs in old.mirror_files([unit]):
                fresh.add(Path(path).read_bytes(), cs, "sha256", Path(rel).name, size)
                refs.append((cs, rel, kind))
            fresh.reconcile_refs(unit, refs)
    recovered = bundle_network(net, cfg, rebuilt, out_path=tmp_path / "recovered.tar",
                               since="2026-11-01", kept_dirs=[first.out_path.parent])
    assert recovered.sent == ordinary.sent == 3
    roots = []
    for name, delta in (("normal", ordinary), ("recovered", recovered)):
        root = tmp_path / name
        home = homes.init_root(root, name="main", set_id=name, network=net.name)
        for result in (first, delta):
            applied = apply_bundle(home, str(result.out_path), say=lambda *a: None)
            assert not applied.refused and not applied.incomplete
        roots.append({p.relative_to(root): p.read_bytes().replace(str(root).encode(), b"ROOT") for p in root.rglob("*")
                      if p.is_file() and ".mmw" not in p.relative_to(root).parts})
    assert roots[0] == roots[1]
