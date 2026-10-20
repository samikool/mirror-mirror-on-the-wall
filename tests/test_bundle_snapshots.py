"""Bundling between any two snapshots — forwards, backwards, skipping, full —
checked end to end: apply.py on the far side must leave exactly the tree a
publish of the destination state laid out."""
import hashlib
import subprocess
import sys
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


def _apply(tmp_path, result, dest) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(tmp_path / "out" / "apply.py"), str(result.out_path),
         "--root", str(dest)], capture_output=True, text=True)


def test_skipping_ahead_and_going_back_land_exactly_on_the_target(states, tmp_path):
    cfg, net, mirror_dir, snaps, published = states
    far = tmp_path / "far"

    full1 = bundle_network(net, cfg, mirror_dir, from_ref=None, to_ref="s1")
    assert full1.full and _apply(tmp_path, full1, far).returncode == 0
    assert _tree(far) == published[1]

    up = bundle_network(net, cfg, mirror_dir, from_ref="s1", to_ref="s3")   # skips s2
    assert (up.previous, up.created) == (snaps[1].stamp, snaps[3].stamp)
    assert _apply(tmp_path, up, far).returncode == 0
    assert _tree(far) == published[3]

    down = bundle_network(net, cfg, mirror_dir, from_ref="s3", to_ref="s1")  # backwards
    r = _apply(tmp_path, down, far)
    assert r.returncode == 0, r.stderr
    assert _tree(far) == published[1], "hello 1.0 must come back — the snapshot kept it"


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


def test_a_default_bundle_snapshots_now(states, tmp_path):
    cfg, net, mirror_dir, snaps, published = states
    first = bundle_network(net, cfg, mirror_dir, from_ref=None)             # to now
    with Pool(mirror_dir, read_only=True) as pool:
        made = next(s for s in pool.snapshots("lab") if s.stamp == first.created)
    assert made.kind == "bundle"
    second = bundle_network(net, cfg, mirror_dir)                           # last → now
    assert second.previous == first.created and second.sent == 0


def test_a_full_bundle_repairs_a_far_side_ahead_of_it(states, tmp_path):
    cfg, net, mirror_dir, snaps, published = states
    far = tmp_path / "far"
    _apply(tmp_path, bundle_network(net, cfg, mirror_dir, from_ref=None, to_ref="s3"), far)
    full2 = bundle_network(net, cfg, mirror_dir, from_ref=None, to_ref="s2")
    assert _apply(tmp_path, full2, far).returncode == 0
    assert _tree(far) == published[2], "hello 3.0 should have been deleted"


def test_refusals(states, tmp_path):
    cfg, net, mirror_dir, snaps, published = states
    with pytest.raises(ValueError, match="same snapshot"):
        bundle_network(net, cfg, mirror_dir, from_ref="s2", to_ref="s2")
    with Pool(mirror_dir) as pool:
        pool.create_snapshot("lab", "20200101T000000Z", kind="imported", complete=False,
                             label="old", rows=[])
    with pytest.raises(ValueError, match="history only"):
        bundle_network(net, cfg, mirror_dir, from_ref="s1", to_ref="old")
    with pytest.raises(LookupError, match="no snapshot"):
        bundle_network(net, cfg, mirror_dir, from_ref="nope", to_ref="s1")


def _status(tmp_path, far) -> str:
    r = subprocess.run([sys.executable, str(tmp_path / "out" / "apply.py"), "--status",
                        "--root", str(far)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_apply_names_both_ends_and_status_reports_where_it_is(states, tmp_path):
    cfg, net, mirror_dir, snaps, published = states
    far = tmp_path / "far"
    full1 = bundle_network(net, cfg, mirror_dir, from_ref=None, to_ref="s1")
    assert "no bundle applied yet" in _status(tmp_path, far)
    r = _apply(tmp_path, full1, far)
    assert r.returncode == 0 and f'{snaps[1].stamp} "s1"' in r.stdout, r.stdout
    assert snaps[1].stamp in _status(tmp_path, far) and '"s1"' in _status(tmp_path, far)

    up = bundle_network(net, cfg, mirror_dir, from_ref="s1", to_ref="s3")
    r = _apply(tmp_path, up, far)
    assert f'{snaps[1].stamp} "s1" -> {snaps[3].stamp} "s3"' in r.stdout, r.stdout


def test_an_out_of_order_bundle_says_exactly_what_to_ask_for(states, tmp_path):
    """The reported failure: a far side that missed a bundle got a diff it
    could not apply, and nothing said what would fix it."""
    cfg, net, mirror_dir, snaps, published = states
    far = tmp_path / "far"
    _apply(tmp_path, bundle_network(net, cfg, mirror_dir, from_ref=None, to_ref="s1"), far)
    wrong = bundle_network(net, cfg, mirror_dir, from_ref="s2", to_ref="s3")
    r = _apply(tmp_path, wrong, far)
    assert r.returncode == 3
    assert f"--from {snaps[1].stamp} --to {snaps[3].stamp}" in r.stderr, r.stderr
    assert "lab" in r.stderr


def test_an_old_one_line_state_file_still_reads(states, tmp_path):
    cfg, net, mirror_dir, snaps, published = states
    far = tmp_path / "far"
    far.mkdir()
    (far / ".mmw-state").write_text(snaps[1].stamp + "\n")      # written by an older apply.py
    up = bundle_network(net, cfg, mirror_dir, from_ref="s1", to_ref="s2")
    assert _apply(tmp_path, up, far).returncode == 0


def test_progress_names_each_mirror_base(states, tmp_path):
    """`mirror_base` is a property, absent from the recorded settings JSON —
    the bases are derived from the recorded mirrors, or none are reported."""
    cfg, net, mirror_dir, snaps, published = states
    calls = []
    bundle_network(net, cfg, mirror_dir, from_ref=None, to_ref="s1",
                   mirror_start_callback=lambda *a: calls.append(a))
    assert calls == [("archive.ubuntu.com/ubuntu", 1, 1)]


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
