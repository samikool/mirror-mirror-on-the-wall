"""Safety gates on the sync path — the two ways a sync could destroy or
corrupt a mirror without saying anything.

1. An upstream-supplied checksum reaching the filesystem as a path component
   (`Pool.add`). Package checksums are computed locally and are safe; metadata
   checksums come straight out of the upstream `Release` SHA256 block, where
   only the *size* field was ever validated.
2. An empty metadata set counting as a clean index flip, so `reconcile_refs`
   replaces the mirror's whole ref set with packages only — deleting every
   index and release ref while reporting success.
"""
import asyncio
import hashlib

import pytest

from mmw.core.downloader import download_mirror
from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool
from mmw.providers.base import MetadataFile, PackageInfo


@pytest.fixture
def mirror_dir(tmp_path):
    return tmp_path / "deep" / "nest" / "mirror"


@pytest.fixture
def pool(mirror_dir):
    p = Pool(mirror_dir)
    yield p
    p.close()


def _pkg(name: str, content: bytes) -> PackageInfo:
    return PackageInfo(
        name=name, url=f"http://x/{name}", checksum=hashlib.sha256(content).hexdigest(),
        checksum_type="sha256", size=len(content), repo_path=f"pool/{name}",
        filename=name, checksum_known=True,
    )


def _seeded_pkg(pool: Pool, name: str, content: bytes) -> PackageInfo:
    """A package already in the pool, so the sync classifies it POOLED and
    never reaches the network — these tests are about the metadata phase."""
    pool.add(content, hashlib.sha256(content).hexdigest(), "sha256", name, len(content))
    return _pkg(name, content)


# ── 1. checksum is never a path ──────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "../../../../pwned",
    "..",
    "a/b",
    "a\\b",
    "",
    "not-hex-at-all",
    "DEADBEEF" * 8,          # upper case is not our canonical form
    "abc",                   # too short to be any digest we emit
    "\x00" + "a" * 63,
])
def test_add_rejects_a_checksum_that_is_not_a_hex_digest(pool, tmp_path, bad):
    """A checksum is interpolated into `packages/{xx}/{checksum}-{name}`. If it
    is allowed to contain `..` or `/`, upstream metadata picks the write path."""
    with pytest.raises(ValueError):
        pool.add(b"MALICIOUS", bad, "sha256", "Packages", 9)

    escaped = [p for p in tmp_path.rglob("*pwned*")] + [p for p in tmp_path.rglob("*MALICIOUS*")]
    assert escaped == [], f"blob escaped the pool: {escaped}"


def test_add_rejects_a_filename_carrying_a_path(pool):
    """Defence in depth: the filename half of the blob name is upstream-derived
    too, and callers reduce it to a basename — this refuses it at the store."""
    good = hashlib.sha256(b"x").hexdigest()
    for bad_name in ("../escape", "a/b", "a\\b"):
        with pytest.raises(ValueError):
            pool.add(b"x", good, "sha256", bad_name, 1)


def test_add_still_accepts_every_digest_length_we_really_emit(pool):
    for data in (b"one", b"two", b"three"):
        for algo in ("md5", "sha1", "sha256", "sha512"):
            digest = hashlib.new(algo, data).hexdigest()
            dest = pool.add(data, digest, algo, "pkg.deb", len(data))
            assert dest.exists()


# ── 2. an empty metadata set must not flip the index ─────────────────────────

def test_empty_metadata_does_not_erase_the_previous_index(pool, mirror_dir):
    """`reconcile_refs` replaces a mirror's refs with exactly what it is given.
    An empty metadata list must therefore abort the flip, not commit packages
    alone — otherwise a CDN 503 on InRelease deletes `dists/` from the served
    tree on the next pruning publish, with the sync reporting success."""
    pkg = _seeded_pkg(pool, "a.deb", b"pkg-bytes")
    rel = MetadataFile(url="u", repo_path="dists/x/InRelease", content=b"rel-bytes", is_release=True)

    first = asyncio.run(download_mirror(
        packages=[pkg], metadata=[rel], pool=pool, mirror_name="m1", max_connections=2,
    ))
    assert first.metadata_ok
    kinds_before = {row[3] for row in _rows(pool, "m1")}
    assert KIND_RELEASE in kinds_before

    second = asyncio.run(download_mirror(
        packages=[pkg], metadata=[], pool=pool, mirror_name="m1", max_connections=2,
    ))

    assert not second.metadata_ok, "an empty metadata set is not a clean index"
    assert second.metadata_errors, "and it must say why"
    assert not second.ok
    kinds_after = {row[3] for row in _rows(pool, "m1")}
    assert KIND_RELEASE in kinds_after, "the previous index must stay in force"
    assert KIND_PKG in kinds_after


def test_a_clean_metadata_set_still_flips_normally(pool, mirror_dir):
    pkg = _seeded_pkg(pool, "a.deb", b"pkg-bytes")
    rel = MetadataFile(url="u", repo_path="dists/x/InRelease", content=b"rel-bytes", is_release=True)
    summary = asyncio.run(download_mirror(
        packages=[pkg], metadata=[rel], pool=pool, mirror_name="m1", max_connections=2,
    ))
    assert summary.metadata_ok and summary.ok
    assert {row[3] for row in _rows(pool, "m1")} == {KIND_PKG, KIND_RELEASE}


def _rows(pool: Pool, mirror_name: str):
    with pool._read() as conn:
        return conn.execute(
            "SELECT checksum, mirror_name, repo_path, kind FROM refs WHERE mirror_name = ?",
            (mirror_name,),
        ).fetchall()


# ── 3. a by-hash response is not taken on trust ──────────────────────────────

class _Resp:
    def __init__(self, status, content=b""):
        self.status_code = status
        self.content = content

    @property
    def is_success(self):
        return 200 <= self.status_code < 300


class _Client:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        return self._responses.pop(0)


def test_by_hash_content_is_verified_against_the_expected_digest(monkeypatch):
    """A by-hash URL whose body is not what its path claims must not be pooled.

    `_fetch_or_skip` short-circuits on `expected_sha256 in pooled`, so a blob
    accepted here is never re-fetched by any later sync and is deduplicated
    into every mirror sharing the file — one bad response would poison the
    pool permanently.
    """
    from mmw.core import downloader

    good = b"the-real-index"
    digest = hashlib.sha256(good).hexdigest()
    # by-hash serves the WRONG bytes; the canonical URL serves the right ones.
    client = _Client([_Resp(200, b"WRONG-BYTES"), _Resp(200, good)])
    monkeypatch.setattr(downloader, "_thread_client", lambda: client)
    monkeypatch.setattr(downloader.time, "sleep", lambda _s: None)

    mf = MetadataFile(url=f"http://x/by-hash/SHA256/{digest}", repo_path="Packages.gz",
                      expected_sha256=digest, fallback_url="http://x/Packages.gz")
    outcome, content, _ = downloader._fetch_by_hash(mf)

    assert content != b"WRONG-BYTES", "a mismatched by-hash body was accepted"
    assert outcome is downloader.Fetch.OK
    assert content == good, "should have fallen through to the canonical URL"


def test_by_hash_backoff_is_interruptible(monkeypatch):
    """The backoff totals ~62 s per file on one of max_connections executor
    threads, so an uninterruptible sleep held the TUI's teardown for minutes."""
    import threading as _t
    from mmw.core import downloader

    stop = _t.Event()
    stop.set()
    mf = MetadataFile(url="http://x/by-hash/SHA256/abc", repo_path="Packages.gz",
                      expected_sha256="abc", fallback_url="http://x/Packages.gz")
    outcome, _, reason = downloader._fetch_by_hash(mf, stop_event=stop)
    assert outcome is downloader.Fetch.FAILED
    assert reason == "stopped"


# ── 4. package retry policy ──────────────────────────────────────────────────

class _StreamResp:
    def __init__(self, status, body=b"", ):
        self.status_code = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_bytes(self, n=65536):
        yield self._body


class _StreamClient:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def stream(self, method, url, headers=None):
        self.calls.append((url, headers))
        return self._responses.pop(0)


def _run_one(monkeypatch, pool, responses, content=b"deb-bytes"):
    from mmw.core import downloader
    client = _StreamClient(responses)
    monkeypatch.setattr(downloader, "_thread_client", lambda: client)
    pkg = _pkg("a.deb", content)
    summary = asyncio.run(download_mirror(
        packages=[pkg],
        metadata=[MetadataFile(url="u", repo_path="dists/x/InRelease", content=b"r", is_release=True)],
        pool=pool, mirror_name="m1", max_connections=1,
    ))
    return client, summary


def test_a_404_is_not_retried(pool, monkeypatch):
    """A package pulled from the archive mid-sync is an answer, not a blip.
    Retrying it twice more just spends round trips confirming it."""
    client, summary = _run_one(monkeypatch, pool, [_StreamResp(404)])
    assert len(client.calls) == 1, f"404 retried {len(client.calls)} times"
    assert summary.failed == 1
    assert "404" in summary.errors[0]


def test_a_transient_failure_is_retried_and_resumes(pool, monkeypatch):
    """A 503 is retried, and the retry carries a Range header so a package that
    dropped at 99% does not cost the whole download again."""
    good = b"deb-bytes"
    client, summary = _run_one(
        monkeypatch, pool,
        [_StreamResp(503), _StreamResp(503), _StreamResp(200, good)],
        content=good,
    )
    assert len(client.calls) == 3
    assert summary.downloaded == 1
    # The first attempt asks for the whole file; later ones may carry a Range
    # (there is nothing on disk here, so the header is absent — what matters is
    # that the retry happened at all and the file landed).
    assert client.calls[0][1] is None


# ── 5. index files are never mistaken for trust roots ────────────────────────

def test_a_prefetched_index_is_meta_not_release(pool):
    """An index that arrives with its bytes already fetched is still an index.

    The release/index split used to be *inferred* — "content is set and
    expected_sha256 is not" meant a release file — which held only because
    `AptProvider` pre-fetched its release files and nothing else pre-fetched
    anything. The moment a provider hands over an index it already has in hand
    (which is the whole point of `fetch_all` fetching it once), that inference
    files `Packages.xz` as a trust root: published LAST, alongside InRelease,
    instead of before it. A client mid-`apt update` would then be able to see a
    release file whose indexes were not there yet — exactly the ordering the
    pkg → meta → release rule exists to guarantee.
    """
    pkg = _seeded_pkg(pool, "a.deb", b"pkg-bytes")
    metadata = [
        MetadataFile(url="u1", repo_path="Packages.xz",
                     content=b"index-bytes"),            # pre-fetched index
        MetadataFile(url="u2", repo_path="InRelease",
                     content=b"rel-bytes", is_release=True),
    ]
    summary = asyncio.run(download_mirror(
        packages=[pkg], metadata=metadata, pool=pool,
        mirror_name="flat", max_connections=2,
    ))
    assert summary.metadata_ok, summary.metadata_errors

    kinds = {row[2]: row[3] for row in _rows(pool, "flat")}
    assert kinds["Packages.xz"] == KIND_META, (
        f"the index was filed as {kinds['Packages.xz']!r}"
    )
    assert kinds["InRelease"] == KIND_RELEASE


# ── 6. two mirrors downloading one URL never share a partial file ────────────

def test_concurrent_downloads_of_one_url_never_pool_a_partial(pool, monkeypatch):
    """`Architecture: all` packages sit in every arch's index, and the TUI
    syncs every mirror at once — so two mirrors download the same URL at the
    same moment. Both used to stream into one URL-keyed resume file: the
    second `open("wb")` replaced the first's, the first then finished and
    linked the SECOND's half-written file into the pool under the right
    checksum, and when the second was stopped the pool kept the truncated
    bytes forever (`_fetch_or_skip`/`batch_contains` never re-fetch a pooled
    checksum)."""
    import threading
    from contextlib import contextmanager

    from mmw.core import downloader as dl

    data = bytes(range(256)) * 1024                   # 256 KiB, 4 chunks
    pkg = _pkg("all.deb", data)
    a_started, b_started = threading.Event(), threading.Event()
    a_go, b_go = threading.Event(), threading.Event()
    stop_b = threading.Event()

    class _Resp:
        status_code = 200

        def __init__(self, started, go):
            self._started, self._go = started, go

        def raise_for_status(self):
            pass

        def iter_bytes(self, n):
            for i in range(0, len(data), 65536):
                yield data[i:i + 65536]
                if i == 0:
                    self._started.set()
                    assert self._go.wait(10)

    class _Client:
        def __init__(self, started, go):
            self._started, self._go = started, go

        @contextmanager
        def stream(self, *a, **k):
            yield _Resp(self._started, self._go)

    outcome: dict[str, object] = {}

    def run(name, started, go, stop):
        dl._thread_local.client = _Client(started, go)
        try:
            outcome[name] = dl._download_sync(pkg, pool, None, None, stop)
        except BaseException as e:            # noqa: BLE001 — recorded for the asserts
            outcome[name] = e

    ta = threading.Thread(target=run, args=("a", a_started, a_go, None))
    tb = threading.Thread(target=run, args=("b", b_started, b_go, stop_b))
    ta.start()
    assert a_started.wait(10)
    tb.start()                                # B opens its file while A is mid-stream
    assert b_started.wait(10)
    a_go.set()
    ta.join(10)                               # A finishes and pools its download
    stop_b.set()
    b_go.set()
    tb.join(10)

    assert outcome["a"].status == dl.Status.DOWNLOADED
    assert isinstance(outcome["b"], InterruptedError)
    pool.flush_writes()
    blob = pool.pool_dir / "packages" / pkg.checksum[:2] / f"{pkg.checksum}-all.deb"
    assert blob.read_bytes() == data, (
        f"the pool holds {blob.stat().st_size} bytes under a {len(data)}-byte package's checksum")
