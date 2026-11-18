"""download_mirror(commit_refs=False): blobs land in the pool, refs stay put,
and the entries the reconcile would have used come back on the summary."""
import asyncio
import hashlib

import httpx
import pytest

from mmw.core import downloader
from mmw.core.downloader import download_mirror
from mmw.core.pool import KIND_PKG, KIND_RELEASE, Pool
from mmw.providers.base import MetadataFile, PackageInfo

BLOBS = {f"http://archive.invalid/pool/{n}": f"{n} bytes".encode() for n in ("a.deb", "b.deb")}


def _pkg(name: str) -> PackageInfo:
    body = BLOBS[f"http://archive.invalid/pool/{name}"]
    return PackageInfo(name=name, url=f"http://archive.invalid/pool/{name}",
                       checksum=hashlib.sha256(body).hexdigest(), checksum_type="sha256",
                       size=len(body), repo_path=f"pool/{name}", filename=name)


RELEASE = [MetadataFile(url="u", repo_path="dists/jammy/InRelease", content=b"rel", is_release=True)]


@pytest.fixture
def pool(tmp_path):
    p = Pool(tmp_path / "mirror")
    yield p
    p.close()


@pytest.fixture
def requests(monkeypatch):
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        body = BLOBS.get(str(request.url))
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(downloader, "_thread_client", lambda: client)
    return seen


def _sync(pool, packages, **kw):
    return asyncio.run(download_mirror(packages=packages, metadata=list(RELEASE), pool=pool,
                                       mirror_name="m1", max_connections=2, **kw))


def test_without_commit_refs_the_refs_stay_and_entries_come_back(pool, requests):
    old = hashlib.sha256(b"old").hexdigest()
    pool.add(b"old", old, "sha256", "old.deb", 3)
    pool.reconcile_refs("m1", [(old, "pool/old.deb", KIND_PKG)])
    before = pool.ref_map("m1")

    summary = _sync(pool, [_pkg("a.deb"), _pkg("b.deb")], commit_refs=False)

    assert summary.ok and summary.downloaded == 2
    assert pool.ref_map("m1") == before
    paths = {(path, kind) for _, path, kind in summary.entries}
    assert {("pool/a.deb", KIND_PKG), ("pool/b.deb", KIND_PKG),
            ("dists/jammy/InRelease", KIND_RELEASE)} == paths
    for checksum, path, kind in summary.entries:
        assert pool.contains(checksum), path


def test_entries_are_set_when_refs_are_committed_too(pool, requests):
    summary = _sync(pool, [_pkg("a.deb")])
    assert summary.ok
    assert pool.ref_map("m1")["pool/a.deb"] == _pkg("a.deb").checksum
    assert {path for _, path, _ in summary.entries} == {"pool/a.deb", "dists/jammy/InRelease"}


def test_blobs_already_in_the_pool_are_not_fetched(pool, requests):
    _sync(pool, [_pkg("a.deb")], commit_refs=False)
    requests.clear()
    summary = _sync(pool, [_pkg("a.deb"), _pkg("b.deb")], commit_refs=False)
    assert summary.pooled == 1 and summary.downloaded == 1
    assert requests == ["http://archive.invalid/pool/b.deb"]
    assert pool.ref_map("m1") == {}
