"""Publishing a snapshot — into the network's root or another directory —
must lay out exactly the tree a publish made at that state, and the pool
remembers what each directory serves so a served snapshot is never deleted."""
import shutil

import pytest
from typer.testing import CliRunner

from mmw import cli
from mmw.core import config
from mmw.core import snapshots as S
from mmw.core.pool import Pool
from mmw.core.publisher import publish_network, publish_snapshot
from mmw.core.publisher.bundle import bundle_network

from test_bundle_snapshots import _tree, states  # noqa: F401 — the shared fixture


def _split(tree: dict[str, bytes]) -> tuple[dict[str, bytes], set[str]]:
    """(files, by-hash paths): by-hash keeps a few stale generations for
    in-flight clients, so a re-published root may hold more of them."""
    files = {p: b for p, b in tree.items() if "/by-hash/" not in p}
    return files, {p for p in tree if "/by-hash/" in p}


def test_publishing_an_older_snapshot_brings_its_packages_back(states, tmp_path):  # noqa: F811
    cfg, net, mirror_dir, snaps, published = states
    root = tmp_path / "published-3"                       # serving s3 now
    publish_snapshot(net.model_copy(update={"root": root}), cfg, mirror_dir, "s1")
    files, by_hash = _split(_tree(root))
    want_files, want_by_hash = _split(published[1])
    assert files == want_files, "hello 1.0 back, hello 3.0 gone, s1's index and release"
    assert want_by_hash <= by_hash


def test_another_directory_leaves_the_root_alone(states, tmp_path):  # noqa: F811
    cfg, net, mirror_dir, snaps, published = states
    root, other = tmp_path / "published-3", tmp_path / "other"
    net = net.model_copy(update={"root": root})
    publish_snapshot(net, cfg, mirror_dir, "s2", root=other)
    assert _tree(root) == published[3]
    assert _tree(other) == published[2]
    conf = (other / "nginx" / "mirror.conf").read_text()
    assert "root /srv/mmw/archive.ubuntu.com;" in conf and str(root) + "/" not in conf


def test_what_each_directory_serves_is_recorded(states, tmp_path):  # noqa: F811
    cfg, net, mirror_dir, snaps, published = states
    root, other = tmp_path / "published-3", tmp_path / "other"
    net = net.model_copy(update={"root": root})
    publish_snapshot(net, cfg, mirror_dir, "s2", root=other)
    with Pool(mirror_dir, read_only=True) as pool:
        serving = {p.root: p.target for p in pool.published("lab")}
    assert serving[str(other.resolve())] == snaps[2].stamp
    assert serving[str(root.resolve())] == "live"         # the fixture's publish


def test_a_served_snapshot_cannot_be_deleted_until_something_else_is(states, tmp_path):  # noqa: F811
    cfg, net, mirror_dir, snaps, published = states
    root = tmp_path / "published-3"
    net = net.model_copy(update={"root": root})
    publish_snapshot(net, cfg, mirror_dir, "s1")
    with Pool(mirror_dir) as pool:
        with pytest.raises(ValueError, match="published at"):
            S.delete_snapshot(pool, S.find_snapshot(pool, "lab", "s1"))
        plan = S.clean_plan(pool, "lab", keep=0)
        assert [s.label for s in plan.delete] == ["s3", "s2"]
        assert [(s.label, "published" in why) for s, why in plan.kept] == [("s1", True)]

    publish_network(net, cfg, mirror_dir)                 # live again
    with Pool(mirror_dir) as pool:
        S.delete_snapshot(pool, S.find_snapshot(pool, "lab", "s1"))
        assert [s.label for s in pool.snapshots("lab")] == ["s3", "s2"]


def test_a_stopped_publish_records_nothing(states, tmp_path):  # noqa: F811
    import threading
    cfg, net, mirror_dir, snaps, published = states
    stop = threading.Event()
    stop.set()
    publish_snapshot(net, cfg, mirror_dir, "s1", root=tmp_path / "x", stop_event=stop)
    with Pool(mirror_dir, read_only=True) as pool:
        assert snaps[1].stamp not in {p.target for p in pool.published("lab")}


def test_a_snapshot_missing_files_is_refused(states, tmp_path):  # noqa: F811
    cfg, net, mirror_dir, snaps, published = states
    shutil.rmtree(mirror_dir / ".pool" / "packages")
    with Pool(mirror_dir) as pool:
        pool._conn.execute("DELETE FROM packages")
        pool._conn.commit()
    with pytest.raises(ValueError, match="no longer in the pool"):
        publish_snapshot(net, cfg, mirror_dir, "s1", root=tmp_path / "x")
    assert not (tmp_path / "x").exists()


def _mmw(monkeypatch, cfg_dir, *args):
    monkeypatch.setattr(cli, "_CFG_DIR", cli._CFG_DIR)   # --config sets it; restored after
    return CliRunner().invoke(cli.app, ["--config", str(cfg_dir), *args])


def test_cli_publish_snapshot_and_delete_guards(states, tmp_path, monkeypatch):  # noqa: F811
    cfg, net, mirror_dir, snaps, published = states
    root = tmp_path / "published-3"
    cfg_dir = tmp_path / "cfg"
    cfg_dir.mkdir()
    config.save(cfg.model_copy(update={"networks": [net.model_copy(update={"root": root})]}),
                cfg_dir)

    r = _mmw(monkeypatch, cfg_dir, "publish", "--snapshot", "s1")
    assert r.exit_code == 1 and "-n" in r.output
    r = _mmw(monkeypatch, cfg_dir, "publish", "-n", "lab", "--snapshot", "s1")
    assert r.exit_code == 0, r.output
    assert _split(_tree(root))[0] == _split(published[1])[0]

    r = _mmw(monkeypatch, cfg_dir, "snapshot", "list", "-n", "lab")
    assert f"published at {root.resolve()}" in r.output
    r = _mmw(monkeypatch, cfg_dir, "snapshot", "delete", "-n", "lab", "s1")
    assert r.exit_code == 1 and "published at" in r.output

    bundle_network(net, cfg, mirror_dir, from_ref=None, to_ref="s2")
    r = _mmw(monkeypatch, cfg_dir, "snapshot", "delete", "-n", "lab", "s2")
    assert r.exit_code == 1 and "--yes" in r.output
    r = _mmw(monkeypatch, cfg_dir, "snapshot", "delete", "-n", "lab", "s2", "--yes")
    assert r.exit_code == 0, r.output

    other = tmp_path / "other"
    r = _mmw(monkeypatch, cfg_dir, "publish", "-n", "lab", "--snapshot", "live",
             "--root", str(other), "--allow-incomplete")   # fixture indexes are fake
    assert r.exit_code == 0, r.output
    assert _split(_tree(other))[0] == _split(published[3])[0]


def test_snapshot_publish_per_base_counts_unchanged(states, tmp_path):  # noqa: F811
    """Pinned from before the shared link loop: per-base accounting is the same."""
    cfg, net, mirror_dir, snaps, published = states
    net = net.model_copy(update={"root": tmp_path / "p"})
    counts = lambda r: [(m.mirror_name, m.linked, m.skipped, m.linked_bytes, m.skipped_bytes,  # noqa: E731
                         len(m.errors)) for m in r.mirrors]
    assert counts(publish_snapshot(net, cfg, mirror_dir, "s1")) == [
        ("archive.ubuntu.com/ubuntu", 5, 0, 39, 0, 0), ("keys and extra files", 1, 0, 38605, 0, 0)]
    assert counts(publish_snapshot(net, cfg, mirror_dir, "s2")) == [
        ("archive.ubuntu.com/ubuntu", 5, 0, 39, 0, 0), ("keys and extra files", 0, 1, 0, 38605, 0)]


def test_snapshot_publish_links_concurrently(states, tmp_path, monkeypatch, disk_limits):  # noqa: F811
    """Serial-fails: the first placement waits until a second one has started."""
    import threading
    from mmw.core.publisher.sinks import TreeSink
    cfg, net, mirror_dir, snaps, published = states
    lock, calls, second, timed_out = threading.Lock(), [0], threading.Event(), []
    real = TreeSink._place

    def place(self, src, rel):
        with lock:
            calls[0] += 1
            n = calls[0]
        if n == 1:
            timed_out.append(not second.wait(5))
        else:
            second.set()
        return real(self, src, rel)

    monkeypatch.setattr(TreeSink, "_place", place)
    disk_limits(write_threads=8)
    publish_snapshot(net.model_copy(update={"root": tmp_path / "p"}), cfg, mirror_dir, "s1")
    assert timed_out == [False]
    files, _ = _split(_tree(tmp_path / "p"))
    assert files == _split(published[1])[0]
