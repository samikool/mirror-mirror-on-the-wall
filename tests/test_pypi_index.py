"""The incremental PyPI index cache: parse, refresh by serial, resume, metadata."""
import threading

from mmw.providers.pypi import index
from mmw.providers.pypi.index import IndexCache, fetch_metadata, parse_project, refresh
from pypi_fakes import FakePyPI, file_entry, metadata_text


def _page(fake, name):
    return {"files": [{k: v for k, v in f.items() if not k.startswith("_")}
                      for f in fake.projects[name]["files"]]}


def _fake(n=3):
    fake = FakePyPI()
    for i in range(n):
        fake.add(f"p{i}", [file_entry(f"p{i}-1.0-py3-none-any.whl", metadata=metadata_text(f"p{i}", "1.0"))])
    return fake


def test_parse_project_reads_pep691_700_fields():
    meta = metadata_text("demo", "2.0")
    entries = [file_entry("Demo-2.0-cp312-cp312-manylinux_2_17_x86_64.whl", requires_python=">=3.9",
                          metadata=meta),
               file_entry("demo-1.0.tar.gz", yanked="broken build")]
    entries[1]["core-metadata"] = False
    entries[1]["data-dist-info-metadata"] = False
    files, skipped = parse_project("Demo", {"files": entries})
    assert skipped == 0
    whl, sdist = files
    assert whl.project == "demo" and whl.version == "2.0" and whl.kind == "wheel"
    assert whl.size == entries[0]["size"] and whl.sha256 == entries[0]["hashes"]["sha256"]
    assert whl.requires_python == ">=3.9" and not whl.yanked
    assert whl.metadata_sha256 == entries[0]["core-metadata"]["sha256"]
    assert whl.repo_path == "files.pythonhosted.org" + entries[0]["url"].split("files.pythonhosted.org")[1]
    assert {t.platform for t in whl.wheel_tags} == {"manylinux_2_17_x86_64"}
    assert sdist.kind == "sdist" and sdist.yanked and sdist.metadata_sha256 is None
    assert sdist.wheel_tags == frozenset()


def test_parse_project_reads_legacy_metadata_key():
    e = file_entry("a-1.0-py3-none-any.whl", metadata=b"x")
    del e["core-metadata"]
    (f,), _ = parse_project("a", {"files": [e]})
    assert f.metadata_sha256 == e["data-dist-info-metadata"]["sha256"]


def test_parse_project_skips_garbage():
    entries = [file_entry("a-1.0.egg"), file_entry("a-1.0.win32.exe"), file_entry("a-notaversion!.tar.gz"),
               file_entry("not_a_wheel.whl"), file_entry("a-1.0-py3-none-any.whl")]
    files, skipped = parse_project("a", {"files": entries})
    assert skipped == 4 and [f.filename for f in files] == ["a-1.0-py3-none-any.whl"]


def test_refresh_fetches_only_changed_serials(tmp_path):
    fake = _fake()
    cache = IndexCache(tmp_path)
    r = refresh(cache, fake.client, threads=2)
    assert r.fetched == 3 and not r.failed and not r.stopped
    fake.projects["p1"]["serial"] = 2
    fake.requests.clear()
    r = refresh(cache, fake.client, threads=2)
    assert r.fetched == 1 and fake.project_requests() == ["p1"]


def test_refresh_drops_removed_projects(tmp_path):
    fake = _fake()
    cache = IndexCache(tmp_path)
    refresh(cache, fake.client, threads=2)
    del fake.projects["p2"]
    r = refresh(cache, fake.client, threads=2)
    assert r.removed == 1 and "p2" not in cache.names() and cache.files("p2") == []


def test_refresh_resumes_after_stop(tmp_path):
    fake = _fake(5)
    stop = threading.Event()
    fake.stop_after = (stop, 2)
    cache = IndexCache(tmp_path)
    r = refresh(cache, fake.client, threads=1, batch=2, stop_event=stop)
    assert r.stopped and r.fetched == 2 and len(cache.names()) == 2
    have = set(cache.names())
    fake.stop_after = None
    fake.requests.clear()
    r = refresh(IndexCache(tmp_path), fake.client, threads=1, batch=2)
    assert r.fetched == 3 and sorted(fake.project_requests()) == sorted(set(fake.projects) - have)


def test_refresh_failure_keeps_old_row(tmp_path):
    fake = _fake()
    cache = IndexCache(tmp_path)
    refresh(cache, fake.client, threads=2)
    old = cache.files("p0")
    fake.projects["p0"]["serial"] = 2
    fake.projects["p0"]["files"] = []
    fake.status["p0"] = 500
    r = refresh(cache, fake.client, threads=2)
    assert r.failed == ["p0"] and cache.files("p0") == old
    assert cache.serials()["p0"] == 1   # retried next time


def test_refresh_404_page_is_an_empty_project(tmp_path):
    fake = _fake(1)
    fake.status["p0"] = 404
    cache = IndexCache(tmp_path)
    r = refresh(cache, fake.client, threads=1)
    assert r.fetched == 1 and not r.failed and cache.files("p0") == []


def test_fetch_metadata_dedups_and_verifies(tmp_path):
    meta = metadata_text("a", "1.0")
    fake = FakePyPI()
    fake.add("a", [file_entry("a-1.0-cp312-cp312-win_amd64.whl", metadata=meta),
                   file_entry("a-1.0-cp312-cp312-manylinux_2_17_x86_64.whl", metadata=meta),
                   file_entry("a-1.0-cp311-cp311-win_amd64.whl", metadata=b"other")])
    fake.bad_metadata.add(fake.projects["a"]["files"][2]["url"])
    cache = IndexCache(tmp_path)
    refresh(cache, fake.client, threads=1)
    fake.requests.clear()
    failures = fetch_metadata(cache, cache.files("a"), fake.client, threads=2)
    assert len([r for r in fake.requests if str(r.url).endswith(".metadata")]) == 2
    assert failures and "a-1.0-cp311-cp311-win_amd64.whl" in failures[0]
    f0 = cache.files("a")[0]
    assert cache.metadata(f0.metadata_sha256) == meta
    assert cache.metadata(cache.files("a")[2].metadata_sha256) is None
    fake.requests.clear()
    fetch_metadata(cache, cache.files("a")[:2], fake.client, threads=2)
    assert not fake.requests   # already cached


def test_cache_survives_reopen(tmp_path):
    fake = _fake()
    cache = IndexCache(tmp_path)
    refresh(cache, fake.client, threads=2)
    cache.close()
    again = IndexCache(tmp_path)
    assert sorted(again.names()) == ["p0", "p1", "p2"] and again.files("p1")[0].version == "1.0"
    assert (tmp_path / index.CACHE_DIR / "index.sqlite").exists()


def test_in_memory_cache():
    cache = IndexCache(None)
    refresh(cache, _fake(1).client, threads=1)
    assert cache.names() == ["p0"]


def test_parse_project_skips_malformed_entries_not_the_project():
    good = file_entry("a-1.0-py3-none-any.whl")
    bad_size = {**file_entry("a-1.1-py3-none-any.whl"), "size": "huge"}
    other_host = {**file_entry("a-1.2-py3-none-any.whl"), "url": "https://evil.example/a-1.2-py3-none-any.whl"}
    dotted = {**file_entry("a-1.3-py3-none-any.whl")}
    dotted["url"] = dotted["url"].replace("/packages/", "/packages/../")
    files, skipped = parse_project("a", {"files": [good, "not a dict", None, bad_size, other_host, dotted]})
    assert [f.filename for f in files] == ["a-1.0-py3-none-any.whl"] and skipped == 5
    assert parse_project("a", ["not", "a", "page"]) == ([], 0)


def test_fetch_metadata_checks_presence_without_reading_bodies(tmp_path, monkeypatch):
    fake = _fake(3)
    cache = IndexCache(tmp_path)
    refresh(cache, fake.client, threads=1)
    files = [f for n in cache.names() for f in cache.files(n)]
    fetch_metadata(cache, files[:2], fake.client, threads=1)
    monkeypatch.setattr(IndexCache, "metadata", lambda self, sha: (_ for _ in ()).throw(AssertionError("body read")))
    fake.requests.clear()
    fetch_metadata(cache, files, fake.client, threads=1)
    assert [str(r.url) for r in fake.requests] == [files[2].url + ".metadata"]
    assert cache.missing_metadata([f.metadata_sha256 for f in files]) == set()


def test_scan_yields_every_project_once(tmp_path):
    fake = _fake(5)
    cache = IndexCache(tmp_path)
    refresh(cache, fake.client, threads=2)
    scanned = list(cache.scan(batch=2))
    assert sorted(n for n, _ in scanned) == cache.names()
    assert all(files == cache.files(n) for n, files in scanned)


def test_a_listing_that_drops_most_of_the_cache_is_refused(tmp_path):
    # A truncated or empty /simple/ (CDN hiccup, proxy cutting the body) must
    # not read as "most of PyPI was deleted".
    fake = _fake(300)
    cache = IndexCache(tmp_path)
    refresh(cache, fake.client, threads=8)
    for name in list(fake.projects)[10:]:
        del fake.projects[name]
    r = refresh(cache, fake.client, threads=8)
    assert r.refused and "290" in r.refused
    assert len(cache.names()) == 300 and r.removed == 0


def test_ordinary_removals_still_apply(tmp_path):
    fake = _fake(300)
    cache = IndexCache(tmp_path)
    refresh(cache, fake.client, threads=8)
    del fake.projects["p7"]
    r = refresh(cache, fake.client, threads=8)
    assert not r.refused and r.removed == 1 and "p7" not in cache.names()


def test_metadata_keeps_the_body_and_its_two_headers(tmp_path):
    meta = metadata_text("a", "1.0", requires=["b>=1", 'c; extra == "x"'], extras=["x"])
    fake = FakePyPI()
    fake.add("a", [file_entry("a-1.0-py3-none-any.whl", metadata=meta)])
    cache = IndexCache(tmp_path)
    refresh(cache, fake.client, threads=1)
    f = cache.files("a")[0]
    fetch_metadata(cache, [f], fake.client, threads=1)
    assert cache.requires(f.metadata_sha256) == (["b>=1", 'c; extra == "x"'], ["x"])
    assert cache.metadata(f.metadata_sha256) == meta      # the sync reads it back, not PyPI
    assert cache.requires("0" * 64) is None


def test_a_cache_from_before_requires_is_backfilled_once(tmp_path):
    import sqlite3
    meta = metadata_text("a", "1.0", requires=["b"])
    sha = index.sha256(meta).hexdigest()
    db = sqlite3.connect(tmp_path / "old.sqlite")
    db.executescript("CREATE TABLE projects (name TEXT PRIMARY KEY, serial INTEGER, files BLOB);"
                     "CREATE TABLE metadata (sha256 TEXT PRIMARY KEY, body BLOB);")
    db.execute("INSERT INTO metadata VALUES (?, ?)", (sha, meta))
    db.commit()
    db.close()
    (tmp_path / index.CACHE_DIR).mkdir()
    (tmp_path / "old.sqlite").rename(tmp_path / index.CACHE_DIR / "index.sqlite")
    cache = IndexCache(tmp_path)
    assert cache.missing_metadata([sha]) == {sha}         # not yet indexed: would be refetched
    stop = threading.Event()
    stop.set()
    assert cache.backfill_requires(stop_event=stop) is False
    said = []
    assert cache.backfill_requires(say=said.append) is True and said
    assert cache.requires(sha) == (["b"], []) and cache.missing_metadata([sha]) == set()
    said.clear()
    assert cache.backfill_requires(say=said.append) is True and not said   # once


def test_a_new_cache_needs_no_backfill(tmp_path):
    cache = IndexCache(tmp_path)
    said = []
    assert cache.backfill_requires(say=said.append) and not said


def test_metadata_order_is_storage_order(tmp_path):
    cache = IndexCache(tmp_path)
    bodies = [metadata_text(f"p{i}", "1.0") for i in range(3)]
    cache.put_metadata([(index.sha256(b).hexdigest(), b) for b in bodies])
    shas = [index.sha256(b).hexdigest() for b in bodies]
    order = cache.metadata_order([*reversed(shas), "f" * 64])
    assert sorted(order, key=order.get) == shas


def test_fetch_metadata_bounds_in_flight(tmp_path):
    """The first request is held: with 4 threads, no more than 4 x 4 requests
    may start before its result is taken — never all 100 up front."""
    import time

    class Gated(FakePyPI):
        def __init__(self):
            super().__init__()
            self.lock, self.started, self.at_first = threading.Lock(), 0, None

        def __call__(self, request):
            if str(request.url).endswith(".metadata"):
                with self.lock:
                    self.started += 1
                    first = self.started == 1
                if first:
                    time.sleep(0.3)
                    self.at_first = self.started
            return super().__call__(request)

    fake = Gated()
    fake.add("a", [file_entry(f"a-{i}.0-py3-none-any.whl", metadata=metadata_text("a", f"{i}.0"))
                   for i in range(100)])
    cache = IndexCache(tmp_path)
    refresh(cache, fake.client, threads=1)
    assert fetch_metadata(cache, cache.files("a"), fake.client, threads=4) == []
    assert fake.at_first is not None and fake.at_first <= 16
    assert all(cache.metadata(f.metadata_sha256) for f in cache.files("a"))


def test_refresh_keeps_fetching_while_rows_commit(tmp_path, monkeypatch):
    """A slow commit no longer stalls the page fetches: rows go to the cache's
    writer thread while refresh keeps requesting."""
    fake = _fake(6)
    all_requested, timed_out = threading.Event(), []
    real_call = fake.__class__.__call__

    def handler(self, request):
        response = real_call(self, request)
        if len(self.project_requests()) >= 6:
            all_requested.set()
        return response

    monkeypatch.setattr(fake.__class__, "__call__", handler)
    cache = IndexCache(tmp_path)
    real_commit = cache._commit

    def slow_commit(items):
        timed_out.append(not all_requested.wait(5))
        real_commit(items)

    monkeypatch.setattr(cache, "_commit", slow_commit)
    r = refresh(cache, fake.client, threads=1, batch=2)
    assert r.fetched == 6 and timed_out and not any(timed_out)
    assert sorted(cache.names()) == [f"p{i}" for i in range(6)]


def test_index_writer_waits_for_every_row_and_pushes_back(tmp_path, monkeypatch):
    """`writing()` stops only once every row is committed (no timeout), and
    its queue is bounded so a slow commit slows the fetchers."""
    from mmw.core.disk_io import writer as writer_mod
    seen = {}
    real_init, real_stop = writer_mod.BatchWriter.__init__, writer_mod.BatchWriter.stop

    def init(self, *a, **k):
        seen["maxsize"] = k.get("maxsize")
        real_init(self, *a, **k)

    def stop(self, timeout=60.0):
        seen["timeout"] = timeout
        return real_stop(self, timeout)

    monkeypatch.setattr(writer_mod.BatchWriter, "__init__", init)
    monkeypatch.setattr(writer_mod.BatchWriter, "stop", stop)
    with IndexCache(tmp_path).writing():
        pass
    assert seen["timeout"] is None and seen["maxsize"]


def test_fetch_metadata_counts_as_it_goes(tmp_path, monkeypatch):
    """A long fetch said nothing until it ended."""
    from itertools import count
    from mmw.providers.pypi import index
    fake = FakePyPI()
    fake.add("a", [file_entry(f"a-1.{i}-py3-none-any.whl", metadata=metadata_text("a", f"1.{i}"))
                   for i in range(3)])
    fake.bad_metadata.add(fake.projects["a"]["files"][2]["url"])
    cache = IndexCache(tmp_path)
    refresh(cache, fake.client, threads=1)
    ticks = count(0, 2)                             # every item is past the 1 s throttle
    monkeypatch.setattr(index.time, "monotonic", lambda: next(ticks))
    msgs = []
    fetch_metadata(cache, cache.files("a"), fake.client, threads=1, say=msgs.append)
    assert msgs == [f"PyPI metadata: fetching the requirements of new releases — {i}/3"
                    for i in (1, 2, 3)] + ["PyPI metadata: 2 fetched, 1 failed"]
