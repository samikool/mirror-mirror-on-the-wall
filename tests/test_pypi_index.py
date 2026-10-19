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
