"""Phase 2: error paths must surface, not vanish."""
import asyncio
import hashlib


from mmw.core.downloader import Fetch, MirrorSummary, _fetch_or_skip, download_mirror
from mmw.core.pool import Pool
from mmw.providers.base import MetadataFile


# ── Fetch outcomes ────────────────────────────────────────────────────────────

def test_prefetched_content_hash_mismatch_fails(tmp_path):
    mf = MetadataFile(url="http://x/f", repo_path="f", content=b"data", expected_sha256="0" * 64)
    outcome, content, reason = _fetch_or_skip(mf, set())
    assert outcome is Fetch.FAILED
    assert content is None
    # the abort message must say why, not just which file
    assert reason and "checksum" in reason


def test_already_pooled_skips(tmp_path):
    data = b"index-bytes"
    sha = hashlib.sha256(data).hexdigest()
    with Pool(tmp_path / "mirror") as pool:
        pool.add(data, sha, "sha256", "primary.xml.gz", len(data))
        mf = MetadataFile(url="http://x/f", repo_path="repodata/primary.xml.gz", expected_sha256=sha)
        # The membership set is what the caller batches up front, and it must
        # include rows still queued for the writer thread.
        outcome, content, reason = _fetch_or_skip(mf, pool.batch_contains([sha]))
    assert outcome is Fetch.ALREADY_DONE
    assert reason is None


# ── download_mirror metadata abort is reported ───────────────────────────────

def _run(coro):
    return asyncio.run(coro)


def test_metadata_verify_failure_reported(tmp_path):
    """A verified metadata file failing its hash must abort the write AND be reported."""
    pool = Pool(tmp_path / "mirror")
    try:
        bad = MetadataFile(
            url="http://x/Packages.gz", repo_path="Packages.gz",
            content=b"stale", expected_sha256="0" * 64,
        )
        release = MetadataFile(url="http://x/InRelease", repo_path="InRelease", content=b"release")
        summary = _run(download_mirror(
            packages=[], metadata=[bad, release],
            pool=pool, mirror_name="m", max_connections=2,
        ))
        assert summary.metadata_ok is False
        assert summary.metadata_errors
        assert not summary.ok
        # No refs reconciled — previous (empty) snapshot stays in force
        assert pool.mirror_files(["m"]) == []
    finally:
        pool.close()


def test_metadata_written_when_all_verify(tmp_path):
    data = b"good-index"
    sha = hashlib.sha256(data).hexdigest()
    pool = Pool(tmp_path / "mirror")
    try:
        good = MetadataFile(url="http://x/Packages.gz", repo_path="Packages.gz",
                            content=data, expected_sha256=sha)
        release = MetadataFile(url="http://x/InRelease", repo_path="InRelease", content=b"release")
        summary = _run(download_mirror(
            packages=[], metadata=[good, release],
            pool=pool, mirror_name="m", max_connections=2,
        ))
        assert summary.metadata_ok is True
        assert summary.ok
        assert pool.resolve("m", "Packages.gz").read_bytes() == data
        assert pool.resolve("m", "InRelease").read_bytes() == b"release"
    finally:
        pool.close()


def test_stop_event_blocks_metadata_commit(tmp_path):
    import threading
    stop = threading.Event()
    stop.set()
    pool = Pool(tmp_path / "mirror")
    try:
        release = MetadataFile(url="http://x/InRelease", repo_path="InRelease", content=b"release")
        summary = _run(download_mirror(
            packages=[], metadata=[release],
            pool=pool, mirror_name="m", max_connections=2,
            stop_event=stop,
        ))
        assert summary.stopped is True
        assert summary.metadata_ok is False
        assert pool.resolve("m", "InRelease") is None  # nothing reconciled
    finally:
        pool.close()


def test_summary_ok_property():
    assert MirrorSummary(mirror="m").ok
    assert not MirrorSummary(mirror="m", failed=1).ok
    assert not MirrorSummary(mirror="m", metadata_ok=False).ok
    assert not MirrorSummary(mirror="m", stopped=True).ok


# ── keys: failures are returned, not swallowed ───────────────────────────────

class _FakeResponse:
    def __init__(self, content=b"key-bytes", status=200):
        self.content = content
        self.status_code = status

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    def __init__(self, fail=False, content=b"key-bytes", status=200):
        self.fail = fail
        self.content = content
        self.status = status

    def get(self, url, **kwargs):
        if self.fail:
            raise ConnectionError("no route to host")
        return _FakeResponse(self.content, self.status)


def test_sync_custom_key_success(tmp_path):
    from mmw.core.keys import sync_custom_key
    r = sync_custom_key("my-mirror", "https://x/repo.key", tmp_path, _FakeClient())
    assert r.ok
    assert r.fetched == 1
    assert (r.dest_dir / "repo.key").read_bytes() == b"key-bytes"
    assert (r.dest_dir / "IMPORT.md").exists()

    # second run: already present
    r2 = sync_custom_key("my-mirror", "https://x/repo.key", tmp_path, _FakeClient())
    assert r2.skipped == 1 and r2.fetched == 0


def test_sync_custom_key_failure_reported(tmp_path):
    from mmw.core.keys import sync_custom_key
    r = sync_custom_key("my-mirror", "https://x/repo.key", tmp_path, _FakeClient(fail=True))
    assert not r.ok
    assert "no route to host" in r.failed[0]
    assert not (r.dest_dir / "repo.key").exists()  # no half-written file


# ── fetch_key: the add-time "paste a URL" method ────────────────────

def test_fetch_key_passes_through_an_already_armoured_key():
    from mmw.core.keys import fetch_key
    armour = "-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nmQINBGXf\n-----END PGP PUBLIC KEY BLOCK-----\n"
    client = _FakeClient(content=armour.encode())
    assert fetch_key("https://x/key.asc", client) == armour


def test_fetch_key_dearmors_a_binary_keyring():
    """A server handing out a raw `.gpg` keyring (the form NVIDIA's own docs
    tell you to produce yourself with `gpg --dearmor`) must come back armoured
    — mmw.toml is a text file, so a key can only be stored there armoured."""
    from mmw.core.keys import fetch_key
    client = _FakeClient(content=b"\x99\x01\x0estray-binary-bytes-not-utf8\xff\xfe")
    armour = fetch_key("https://x/key.gpg", client)
    assert armour.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")


def test_fetch_key_raises_on_http_failure():
    from mmw.core.keys import fetch_key
    import pytest
    with pytest.raises(RuntimeError):
        fetch_key("https://x/key.asc", _FakeClient(status=404))


def test_detect_unreachable_carries_error(monkeypatch):
    import httpx
    from mmw.core import discover

    class BoomClient:
        def __init__(self, *a, **kw): ...
        def __enter__(self): return self
        def __exit__(self, *a): ...
        def head(self, url): raise httpx.ConnectError("connection refused")
        def get(self, url): raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "Client", BoomClient)
    result = discover.detect_repository("http://nowhere.invalid/repo")
    assert result.provider is None
    assert "refused" in result.error


# ── transient upstream errors must not discard a whole index ─────────────────

class _StubResp:
    def __init__(self, status_code, content=b""):
        self.status_code, self.content = status_code, content

    @property
    def is_success(self):
        return 200 <= self.status_code < 300


class _StubClient:
    """Returns the queued responses in order, recording how many calls it saw."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        r = self._responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_by_hash_retries_transient_5xx(monkeypatch):
    """A busy mirror answering 503 once must not throw away the mirror's index —
    5xx/429 are transient and get the same backoff as a by-hash 404."""
    from mmw.core import downloader

    data = b"index-content"
    client = _StubClient([_StubResp(503), _StubResp(429), _StubResp(200, data)])
    monkeypatch.setattr(downloader, "_thread_client", lambda: client)
    monkeypatch.setattr(downloader.time, "sleep", lambda _s: None)

    mf = MetadataFile(url="http://x/by-hash/SHA256/abc", repo_path="Packages.gz",
                      expected_sha256="abc", fallback_url="http://x/Packages.gz")
    outcome, content, reason = downloader._fetch_by_hash(mf)
    assert outcome is Fetch.OK
    assert content == data
    assert len(client.calls) == 3          # retried past both transient codes
    assert reason is None


def test_by_hash_does_not_retry_a_real_client_error(monkeypatch):
    """403 is an answer, not a blip — stop asking, and say what happened."""
    from mmw.core import downloader

    client = _StubClient([_StubResp(403)])
    monkeypatch.setattr(downloader, "_thread_client", lambda: client)
    monkeypatch.setattr(downloader.time, "sleep", lambda _s: None)

    mf = MetadataFile(url="http://x/by-hash/SHA256/abc", repo_path="Packages.gz",
                      expected_sha256="abc", fallback_url="http://x/Packages.gz")
    outcome, _content, reason = downloader._fetch_by_hash(mf)
    assert outcome is Fetch.FAILED
    assert len(client.calls) == 1
    assert reason == "http 403"


def test_metadata_abort_message_names_the_reason(tmp_path):
    """The abort has to say WHY, not just which file — otherwise a failed sync
    is undiagnosable after the fact."""
    pool = Pool(tmp_path / "mirror")
    try:
        bad = MetadataFile(url="http://x/Packages.gz", repo_path="Packages.gz",
                           content=b"stale", expected_sha256="0" * 64)
        release = MetadataFile(url="http://x/InRelease", repo_path="InRelease", content=b"release")
        summary = _run(download_mirror(
            packages=[], metadata=[bad, release],
            pool=pool, mirror_name="m", max_connections=2,
        ))
        assert summary.metadata_ok is False
        msg = " ".join(summary.metadata_errors)
        assert "Packages.gz" in msg and "checksum" in msg
    finally:
        pool.close()


def test_stale_index_is_resnapshotted_not_failed(tmp_path):
    """A long package phase can outlive the Release snapshot: upstream prunes the
    old by-hash entries and the canonical files no longer match our hashes.  That
    must re-read the index and commit the newer one, not abort with a warning."""
    fresh = b"fresh-index"
    fresh_sha = hashlib.sha256(fresh).hexdigest()
    calls = []

    def refetch():
        calls.append(1)
        return [
            MetadataFile(url="http://x/Packages.gz", repo_path="Packages.gz",
                         content=fresh, expected_sha256=fresh_sha),
            MetadataFile(url="http://x/InRelease", repo_path="InRelease", content=b"new-release"),
        ]

    pool = Pool(tmp_path / "mirror")
    try:
        # what we started the sync with — upstream has moved on since
        stale = MetadataFile(url="http://x/Packages.gz", repo_path="Packages.gz",
                             content=b"stale", expected_sha256="0" * 64)
        release = MetadataFile(url="http://x/InRelease", repo_path="InRelease", content=b"old-release")
        summary = _run(download_mirror(
            packages=[], metadata=[stale, release],
            pool=pool, mirror_name="m", max_connections=2,
            refetch_metadata=refetch,
        ))
        assert calls == [1]                          # re-read exactly once
        assert summary.metadata_ok is True
        assert summary.metadata_resnapshotted is True
        assert summary.ok                            # green card, not a warning
        assert not summary.metadata_errors           # stale-attempt noise discarded
        assert pool.resolve("m", "Packages.gz").read_bytes() == fresh
        assert pool.resolve("m", "InRelease").read_bytes() == b"new-release"
    finally:
        pool.close()


def test_resnapshot_failing_twice_still_reports(tmp_path):
    """If the second read fails too, it's a real problem — report it."""
    def refetch():
        return [MetadataFile(url="http://x/Packages.gz", repo_path="Packages.gz",
                             content=b"still-wrong", expected_sha256="1" * 64)]

    pool = Pool(tmp_path / "mirror")
    try:
        stale = MetadataFile(url="http://x/Packages.gz", repo_path="Packages.gz",
                             content=b"stale", expected_sha256="0" * 64)
        summary = _run(download_mirror(
            packages=[], metadata=[stale],
            pool=pool, mirror_name="m", max_connections=2,
            refetch_metadata=refetch,
        ))
        assert summary.metadata_ok is False
        assert summary.metadata_errors
        assert pool.mirror_files(["m"]) == []
    finally:
        pool.close()
