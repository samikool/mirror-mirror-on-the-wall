"""Phase B: sync fills the pool and reconciles refs — no trees."""
import asyncio
import hashlib
import threading

import pytest

from mmw.core.downloader import Status, download_mirror
from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool
from mmw.providers.base import MetadataFile, PackageInfo


def _pkg(name: str, content: bytes, repo_path: str | None = None, known: bool = True) -> PackageInfo:
    checksum = hashlib.sha256(content).hexdigest() if known else ""
    return PackageInfo(
        name=name, url=f"http://x/{name}", checksum=checksum, checksum_type="sha256",
        size=len(content), repo_path=repo_path or f"pool/{name}", filename=name,
        checksum_known=known,
    )


def _seed(pool: Pool, content: bytes, name: str) -> str:
    checksum = hashlib.sha256(content).hexdigest()
    pool.add(content, checksum, "sha256", name, len(content))
    return checksum


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def mirror_dir(tmp_path):
    return tmp_path / "mirror"


@pytest.fixture
def pool(mirror_dir):
    p = Pool(mirror_dir)
    yield p
    p.close()


def _sync(pool, mirror_dir, packages, metadata=(), **kw):
    return _run(download_mirror(
        packages=list(packages), metadata=list(metadata), pool=pool,
        mirror_name="m1",
        max_connections=2, **kw,
    ))


def test_pooled_packages_classified_and_reconciled(pool, mirror_dir):
    content = b"already-here"
    _seed(pool, content, "a.deb")
    pkg = _pkg("a.deb", content)

    events = []
    phases = []
    summary = _sync(pool, mirror_dir, [pkg],
                    [MetadataFile(url="u", repo_path="InRelease", content=b"rel", is_release=True)],
                    phase_callback=phases.append,
                    progress=lambda n, r: events.append((n, r.status)))

    assert summary.pooled == 1 and summary.ok
    assert events[0] == ("a.deb", Status.POOLED)
    assert phases[-2] == "Committing pool references..."
    assert phases[-1] == f"Pool references committed in {summary.reconcile_seconds:.2f}s"
    assert summary.reconcile_seconds >= 0
    assert {"pool lookup", "package classification", "metadata lookup", "metadata verification",
            "metadata writes", "other metadata", "reference commit"} == set(summary.timings)
    # refs reconciled: pkg + release entries
    rows = pool.mirror_files(["m1"])
    by_kind = {kind: path for path, _, _, kind, _ in rows}
    assert by_kind[KIND_PKG] == "pool/a.deb"
    assert by_kind[KIND_RELEASE] == "InRelease"
    # nothing written outside the pool
    assert not (mirror_dir / "example.com").exists()


def test_release_blob_pooled_with_computed_checksum(pool, mirror_dir):
    rel_content = b"release-bytes"
    _sync(pool, mirror_dir, [], [MetadataFile(url="u", repo_path="InRelease", content=rel_content, is_release=True)])
    p = pool.resolve("m1", "InRelease")
    assert p is not None and p.read_bytes() == rel_content


def test_verified_metadata_pooled_and_deduped(pool, mirror_dir):
    data = b"shared-suite-index"
    sha = hashlib.sha256(data).hexdigest()
    mf = MetadataFile(url="u", repo_path="dists/bw/InRelease.sub", content=data, expected_sha256=sha)
    rel = MetadataFile(url="u2", repo_path="dists/bw/InRelease", content=b"rel", is_release=True)

    s1 = _sync(pool, mirror_dir, [], [mf, rel])
    assert s1.metadata_ok
    assert pool.contains(sha)

    # second mirror sharing the file: ALREADY_DONE, still gets its own ref
    s2 = _run(download_mirror(
        packages=[], metadata=[MetadataFile(url="u", repo_path="dists/bw/InRelease.sub",
                                            expected_sha256=sha),
                               MetadataFile(url="u2", repo_path="dists/bw/InRelease", content=b"rel", is_release=True)],
        pool=pool, mirror_name="m2", max_connections=2,
    ))
    assert s2.metadata_ok
    assert pool.resolve("m2", "dists/bw/InRelease.sub") is not None


def test_failed_verification_blocks_reconcile(pool, mirror_dir):
    old = _seed(pool, b"old-index", "Packages.xz")
    pool.reconcile_refs("m1", [(old, "dists/Packages.xz", KIND_META)])

    bad = MetadataFile(url="u", repo_path="dists/Packages.xz",
                       content=b"new", expected_sha256="0" * 64)
    summary = _sync(pool, mirror_dir, [], [bad])
    assert summary.metadata_ok is False
    # previous snapshot still in force
    assert pool.resolve("m1", "dists/Packages.xz").read_bytes() == b"old-index"


def test_stop_blocks_reconcile(pool, mirror_dir):
    stop = threading.Event()
    stop.set()
    content = b"x"
    _seed(pool, content, "a.deb")
    summary = _sync(pool, mirror_dir, [_pkg("a.deb", content)],
                    [MetadataFile(url="u", repo_path="InRelease", content=b"rel", is_release=True)],
                    stop_event=stop)
    assert summary.stopped and not summary.metadata_ok
    assert pool.mirror_files(["m1"]) == []  # nothing reconciled


def test_unsafe_metadata_path_blocks_reconcile(pool, mirror_dir):
    evil = MetadataFile(url="u", repo_path="../../evil", content=b"evil")
    summary = _sync(pool, mirror_dir, [], [evil])
    assert summary.metadata_ok is False
    assert pool.mirror_files(["m1"]) == []


def test_failed_download_still_wanted(pool, mirror_dir):
    """A package listed upstream but not obtainable stays in refs (desired state)."""
    pkg = _pkg("gone.deb", b"unreachable")  # http://x/ is unfetchable in tests
    summary = _sync(pool, mirror_dir, [pkg],
                    [MetadataFile(url="u", repo_path="InRelease", content=b"rel", is_release=True)])
    assert summary.failed == 1
    assert summary.metadata_ok  # metadata fine; the mirror is just incomplete
    assert pool.missing_for("m1") == [("pool/gone.deb", pkg.checksum)]


def test_superseded_refs_dropped_on_next_sync(pool, mirror_dir):
    v1 = _seed(pool, b"v1", "p_1.deb")
    v2 = _seed(pool, b"v2", "p_2.deb")
    rel = [MetadataFile(url="u", repo_path="InRelease", content=b"rel", is_release=True)]

    _sync(pool, mirror_dir, [_pkg("p_1.deb", b"v1")], rel)
    _sync(pool, mirror_dir, [_pkg("p_2.deb", b"v2")], rel)  # upstream replaced v1

    paths = {r[0] for r in pool.mirror_files(["m1"], kinds=(KIND_PKG,))}
    assert paths == {"pool/p_2.deb"}
    # v1 blob is now orphaned and prunable
    removed, _ = pool.prune_orphans(["m1"])
    assert removed == 1
    assert not pool.contains(v1)
    assert pool.contains(v2)


def test_legacy_paths_remembered_via_refs(pool, mirror_dir):
    """rpm-legacy: no upstream checksums — refs are the path memory."""
    content = b"legacy-rpm"
    checksum = _seed(pool, content, "old.rpm")
    pool.reconcile_refs("m1", [(checksum, "en/old.rpm", KIND_PKG)])

    pkg = _pkg("old.rpm", content, repo_path="en/old.rpm", known=False)
    events = []
    summary = _sync(pool, mirror_dir, [pkg],
                    progress=lambda n, r: events.append((n, r.status)))
    assert summary.skipped == 1
    assert summary.downloaded == 0
    assert events[0] == ("old.rpm", Status.SKIPPED)
    # ref survives reconciliation with its remembered checksum
    assert pool.ref_map("m1") == {"en/old.rpm": checksum}


def test_no_tree_artifacts_anywhere(pool, mirror_dir):
    """After a sync, mirror_dir contains only .pool (and whatever keys/ adds)."""
    content = b"bytes"
    _seed(pool, content, "a.deb")
    _sync(pool, mirror_dir, [_pkg("a.deb", content)],
          [MetadataFile(url="u", repo_path="dists/bw/InRelease", content=b"rel", is_release=True)])
    children = {p.name for p in mirror_dir.iterdir()}
    assert children == {".pool"}
