"""Snapshots of network `lab` at three states, and bundles to and from them:
the `states` fixture other tests share."""
import hashlib
from pathlib import Path

import pytest

from mmw.core import config
from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool
from mmw.core.publisher import publish_network
from mmw.core.publisher.bundle import bundle_network
from mmw.core.snapshots import take_snapshot

SRC = "https://archive.ubuntu.com/ubuntu"
MIRROR = "ubuntu-noble-main-amd64"


def _state(mirror_dir: Path, v: int) -> None:
    """The pool as a sync at version `v` leaves it: hello_{v}.0 and an index naming it."""
    with Pool(mirror_dir) as pool:
        rows = [
            (f"pool/main/h/hello/hello_{v}.0_amd64.deb", f"hello {v}".encode(), KIND_PKG),
            ("dists/noble/main/binary-amd64/Packages.gz", f"index {v}".encode(), KIND_META),
            ("dists/noble/InRelease", f"release {v}".encode(), KIND_RELEASE),
        ]
        refs = []
        for repo_path, content, kind in rows:
            cs = hashlib.sha256(content).hexdigest()
            pool.add(content, cs, "sha256", Path(repo_path).name, len(content))
            refs.append((cs, repo_path, kind))
        pool.reconcile_refs(MIRROR, refs, source=SRC)
        pool.record_mirror(config.MirrorConfig(
            name=MIRROR, source=SRC, provider="apt", source_url=SRC, arch="amd64",
            suite="noble", component="main", label="ubuntu"))


def _tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in (root / "archive.ubuntu.com").rglob("*") if p.is_file()}


@pytest.fixture
def states(tmp_path):
    """Snapshots s1, s2, s3 of network `lab`, and what a publish laid out at each."""
    mirror_dir = tmp_path / "mirror"
    cfg = config.Config(settings=config.Settings(mirror_dir=mirror_dir),
                        sources=[config.SourceConfig(url=SRC, suites=["noble"],
                                                     components=["main"], arches=["amd64"])])
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out", certs=False)  # snapshots, not TLS
    snaps, published = {}, {}
    for v in (1, 2, 3):
        _state(mirror_dir, v)
        snaps[v] = take_snapshot(net, cfg, mirror_dir, label=f"s{v}")
        root = tmp_path / f"published-{v}"
        publish_network(net.model_copy(update={"root": root}), cfg, mirror_dir)
        published[v] = _tree(root)
    # Upstream has moved on: prune drops everything only the snapshots hold.
    with Pool(mirror_dir) as pool:
        pool.prune_orphans([SRC])
    return cfg, net, mirror_dir, snaps, published


def test_the_default_starting_point_is_where_the_last_bundle_went(states, tmp_path):
    cfg, net, mirror_dir, snaps, published = states
    bundle_network(net, cfg, mirror_dir, from_ref=None, to_ref="s3")
    bundle_network(net, cfg, mirror_dir, from_ref="s3", to_ref="s1")       # backwards
    nxt = bundle_network(net, cfg, mirror_dir, to_ref="s2")                 # default from
    assert nxt.previous == snaps[1].stamp
    with Pool(mirror_dir, read_only=True) as pool:
        log = pool.bundles("lab")
    assert [(b.from_stamp, b.to_stamp) for b in log] == [
        (snaps[1].stamp, snaps[2].stamp), (snaps[3].stamp, snaps[1].stamp),
        (None, snaps[3].stamp)]


def test_a_damaged_blob_refuses_the_bundle_and_records_nothing(states, tmp_path):
    """Recording a bundle that left a file out made the next delta treat the
    file as sent: deltas come from the destination snapshot, which names it.
    So a damaged pool blob refuses the bundle whole — no tar, no log entry,
    and the default starting point of the next bundle is unchanged."""
    cfg, net, mirror_dir, snaps, _published = states
    bundle_network(net, cfg, mirror_dir, from_ref=None, to_ref="s1")
    content = b"hello 3"
    cs = hashlib.sha256(content).hexdigest()
    (mirror_dir / ".pool" / "packages" / cs[:2] / f"{cs}-hello_3.0_amd64.deb").write_bytes(b"he")

    with pytest.raises(ValueError, match="damaged in the pool"):
        bundle_network(net, cfg, mirror_dir, to_ref="s3")
    with Pool(mirror_dir, read_only=True) as pool:
        assert [b.to_stamp for b in pool.bundles("lab")] == [snaps[1].stamp]
    assert not list((tmp_path / "out").glob(f"*{snaps[3].stamp}.tar"))
