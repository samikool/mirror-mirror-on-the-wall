"""Stage-3 final-review findings, each pinned by the test that reproduces it."""
import asyncio
import concurrent.futures
import hashlib
import json

import httpx
import pytest

from mmw.core import config, downloader
from mmw.core.pool import KIND_PKG, Pool
from mmw.providers import get_provider
from mmw.providers.vscode import marketplace as mp
from mmw.providers.vscode import provider as prov
from mmw.providers.vscode import updates
from vscode_fakes import FakeUpstream

APT = "https://archive.ubuntu.com/ubuntu"


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(mp, "BACKOFF", 0.0)
    monkeypatch.setattr(mp, "RATE_LIMIT_WAIT", 0.0)
    monkeypatch.setattr(updates, "BACKOFF", 0.0, raising=False)


@pytest.fixture
def up(monkeypatch):
    fake = FakeUpstream()
    monkeypatch.setattr(prov, "_client", fake.client)
    monkeypatch.setattr(downloader, "_thread_client", fake.client)
    return fake


def _vs(**kw):
    kw.setdefault("extensions", config.ExtensionSelection(ids=["ms-python.python"]))
    return config.VscodeSource(provider="vscode", **kw)


def _seed(pool, unit, path="x/y", body=b"x"):
    cs = hashlib.sha256(body).hexdigest()
    pool.add(body, cs, "sha256", path.rsplit("/", 1)[-1], len(body))
    pool.reconcile_refs(unit.name, [(cs, path, KIND_PKG)], source=unit.source)
    pool.record_mirror(unit)


# ── Critical 1: a network without `select` keeps publishing apt ──────────────

def _mixed(tmp_path):
    apt_src = config.SourceConfig(url=APT, suites=["noble"], components=["main"], arches=["amd64"])
    vs = _vs(kinds=["server"], platforms=["linux-x64"], qualities=["stable"])
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"), sources=[apt_src, vs])
    with Pool(cfg.settings.mirror_dir) as pool:
        (apt_unit,) = apt_src.to_mirrors()
        _seed(pool, apt_unit, "pool/main/h/hello.deb", b"deb")
        _seed(pool, get_provider("vscode").expand(vs)[0], "h.example/build", b"build")
    return cfg


def _with_fake_docker(tmp_path, monkeypatch):
    from mmw.providers.vscode import image
    from test_gallery_image import _fake_docker
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(image, "DOCKER", _fake_docker(bin_dir)[0])


def test_a_network_without_select_publishes_apt_and_vscode(tmp_path, monkeypatch):
    """Stage 3 skipped VS Code units in an implicit network; stage 4 publishes them."""
    from mmw.core.certs import collect_hostnames
    from mmw.core.publisher import publish_network
    from mmw.core.snapshots import take_snapshot
    _with_fake_docker(tmp_path, monkeypatch)
    cfg = _mixed(tmp_path)
    net = config.NetworkConfig(name="lab", root=tmp_path / "pub")
    result = publish_network(net, cfg, cfg.settings.mirror_dir)
    assert not [e for r in result.mirrors for e in r.errors]
    assert (tmp_path / "pub" / "archive.ubuntu.com").exists()
    assert (tmp_path / "pub" / "h.example" / "build").exists()
    assert (tmp_path / "pub" / "vscode-gallery").is_dir()
    take_snapshot(net, cfg, cfg.settings.mirror_dir)              # does not raise
    assert "marketplace.visualstudio.com" in collect_hostnames(net, cfg)


def test_selecting_vscode_explicitly_publishes_it(tmp_path, monkeypatch):
    from mmw.core.publisher import publish_network
    _with_fake_docker(tmp_path, monkeypatch)
    cfg = _mixed(tmp_path)
    net = config.NetworkConfig(name="lab", root=tmp_path / "pub", select=[
        config.NetworkSelect(source=config.MARKETPLACE_URL)])
    result = publish_network(net, cfg, cfg.settings.mirror_dir)
    assert not [e for r in result.mirrors for e in r.errors]
    assert (tmp_path / "pub" / "h.example" / "build").exists()
    assert not (tmp_path / "pub" / "archive.ubuntu.com").exists()


# ── Important 2: a network failure is reported, not a traceback ──────────────

def _failing_client():
    def boom(request):
        raise httpx.ConnectError("no route to host", request=request)
    return httpx.Client(transport=httpx.MockTransport(boom))


def test_a_transport_error_is_reported_and_other_sources_still_resolve(monkeypatch):
    from mmw.core import discover
    from mmw.core.resolve import resolve_sources
    monkeypatch.setattr(prov, "_client", _failing_client)
    monkeypatch.setattr(discover, "list_suites", lambda url: ["noble"])
    apt_src = config.SourceConfig(url=APT, suites=["noble"], components=["main"], arches=["amd64"])
    mirrors, notes, errors = resolve_sources([apt_src, _vs()])
    assert any(m.provider == "apt" for m in mirrors)
    assert any("no route to host" in e for e in errors)


def test_transport_errors_are_retried_then_raised_as_marketplace_errors():
    with pytest.raises(mp.MarketplaceError, match="no route"):
        mp.query(_failing_client(), [], page_size=1)


def test_a_provider_exception_becomes_a_resolve_error(monkeypatch):
    from mmw.core.resolve import resolve_sources

    def broken(self, source, *, progress=None):
        raise ValueError("kaboom")
    monkeypatch.setattr(prov.VscodeProvider, "resolve", broken)
    _, _, errors = resolve_sources([_vs()])
    assert any("kaboom" in e for e in errors)


# ── Important 3: paging keeps a constant page size ───────────────────────────

class _PagedGallery:
    """2000 extensions, honouring pageNumber × pageSize offsets."""
    def __init__(self):
        self.requests = []
        self.items = [{"publisher": {"publisherName": "p"}, "extensionName": f"e{i:04d}",
                       "versions": []} for i in range(2000)]

    def __call__(self, request):
        f = json.loads(request.content)["filters"][0]
        self.requests.append((f["pageNumber"], f["pageSize"]))
        start = (f["pageNumber"] - 1) * f["pageSize"]
        page = self.items[start:start + f["pageSize"]]
        return httpx.Response(200, json={"results": [{"extensions": page, "resultMetadata": [
            {"metadataType": "ResultCount", "metadataItems": [{"name": "TotalCount", "count": 2000}]}]}]})


@pytest.mark.parametrize("n,pages", [(700, 2), (1100, 3), (1999, 4), (300, 1)])
def test_top_n_pages_with_a_constant_size(n, pages):
    g = _PagedGallery()
    got = mp.top_n(httpx.Client(transport=httpx.MockTransport(g)), n)
    assert [mp.ext_id(e) for e in got] == [f"p.e{i:04d}" for i in range(n)]
    assert len(g.requests) == pages and len({s for _, s in g.requests}) == 1


def test_search_all_reads_every_page_once():
    g = _PagedGallery()
    assert len(mp.search_all(httpx.Client(transport=httpx.MockTransport(g)))) == 2000
    assert [p for p, _ in g.requests] == [1, 2, 3, 4]


# ── Important 4: re-query whenever a prerelease is listed ────────────────────

def _v(version, platform=None, pre=False):
    v = {"version": version, "files": [{"assetType": "VSIX", "source":
         f"https://p.gallerycdn.vsassets.io/x/{version}/{platform or 'any'}/VSIX"}], "properties": []}
    if platform:
        v["targetPlatform"] = platform
    if pre:
        v["properties"].append({"key": mp.PRERELEASE_KEY, "value": "true"})
    return v


def _two_answer_client(latest, full):
    def handler(request):
        if request.url.host == "main.vscode-cdn.net":
            return httpx.Response(200, json={"malicious": []})
        body = json.loads(request.content)
        ext = {"publisher": {"publisherName": "p"}, "extensionName": "e",
               "versions": full if body["flags"] == mp.FLAGS_ALL_VERSIONS else latest}
        return httpx.Response(200, json={"results": [{"extensions": [ext], "resultMetadata": []}]})
    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


def test_a_mixed_latest_only_answer_is_requeried(monkeypatch):
    latest = [_v("2.0.0", "linux-x64", pre=True), _v("1.9.0", "win32-x64"), _v("1.0.0")]
    # Every version, newest first as the gallery lists them.
    full = [latest[0], _v("1.9.0", "win32-x64"), _v("1.9.0", "linux-x64"), _v("1.0.0")]
    monkeypatch.setattr(prov, "_client", _two_answer_client(latest, full))
    src = _vs(kinds=["extensions"], platforms=["linux-x64"],
              extensions=config.ExtensionSelection(ids=["p.e"]))
    get_provider("vscode").resolve(src)
    ext = get_provider("vscode").catalog[src.identity]["p.e"]
    assert [v["version"] for v in mp.pick_versions(ext, "linux-x64", prerelease=False)] == ["1.9.0"]


def test_all_versions_queries_use_smaller_batches():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content)["flags"])
        return httpx.Response(200, json={"results": [{"extensions": [], "resultMetadata": []}]})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    mp.by_names(client, [f"p.e{i}" for i in range(25)], flags=mp.FLAGS_ALL_VERSIONS)
    assert len(seen) == 3


# ── Important 5 + minor: universal builds live in one unit, once ─────────────

def test_universal_builds_are_fetched_by_one_unit_only(up):
    vs = get_provider("vscode")
    src = _vs(kinds=["extensions"], platforms=["linux-x64", "win32-x64", "darwin-arm64"])
    units = vs.expand(src)
    assert "vscode-extensions-universal" in {u.name for u in units}
    universal = next(u for u in units if u.name == "vscode-extensions-universal")
    assert universal.dims == {"kind": "extensions"}
    assert config.NetworkSelect(source=config.MARKETPLACE_URL, platforms=["linux-x64"]).matches(universal)
    vs.resolve(src)
    seen: dict[str, str] = {}
    releases = []
    for u in units:
        pkgs, meta = vs.fetch_all(u, up.client())
        releases += [m for m in meta if m.is_release]
        for p in pkgs:
            assert p.repo_path not in seen, (p.repo_path, seen.get(p.repo_path), u.name)
            seen[p.repo_path] = u.name
    assert seen                                           # something was fetched
    assert len(releases) == 1                             # malicious list: once per source


# ── Important 6: a gallery failure keeps the build units ─────────────────────

def test_a_gallery_failure_keeps_desktop_and_server_units(monkeypatch):
    monkeypatch.setattr(prov, "_client", _failing_client)
    r = get_provider("vscode").resolve(_vs(platforms=["linux-x64"], qualities=["stable"]))
    assert r.errors
    assert {u.dims["kind"] for u in r.mirrors} == {"desktop", "server"}


# ── Important 7: known-malicious extensions are never mirrored ───────────────

def test_malicious_extensions_are_dropped(up, monkeypatch):
    real = up.__call__

    def handler(request):
        if request.url.host == "main.vscode-cdn.net":
            return httpx.Response(200, json={"malicious": ["MS-Python.debugpy"]})
        return real(request)
    monkeypatch.setattr(prov, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    src = _vs(kinds=["extensions"], platforms=["linux-x64"], extensions=config.ExtensionSelection(
        ids=["ms-python.python", "ms-python.debugpy"]))
    r = get_provider("vscode").resolve(src)
    assert "ms-python.debugpy" not in get_provider("vscode").catalog[src.identity]
    assert any("malicious" in n and "ms-python.debugpy" in n for n in r.notes)


# ── Important 8: dry-run after a sync downloads nothing ──────────────────────

def _sync(tmp_path, src):
    from mmw.core.resolve import resolve_sources
    from mmw.core.sync_engine import sync_mirror
    units, _, errors = resolve_sources([src])
    assert not errors
    ex = concurrent.futures.ThreadPoolExecutor(4)

    async def go():
        with Pool(tmp_path) as pool:
            for u in units:
                assert (await sync_mirror(u, pool=pool, mirror_dir=tmp_path, max_connections=4,
                                          executor=ex)).ok
    try:
        asyncio.run(go())
    finally:
        ex.shutdown()
    return units


def test_dry_run_after_a_sync_has_nothing_to_fetch(up, tmp_path):
    from mmw.core.sync_engine import classify_mirror
    units = _sync(tmp_path, _vs(kinds=["extensions"], platforms=["linux-x64"]))
    ex = concurrent.futures.ThreadPoolExecutor(2)
    try:
        with Pool(tmp_path, read_only=True) as pool:
            for u in units:
                stats = asyncio.run(classify_mirror(u, pool, ex))
                assert stats.to_fetch == 0, (u.name, stats)
    finally:
        ex.shutdown()


# ── minors ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://h.example/a/./b", "https://h.example/a//b", "https://h.example/a/../b",
    "https://h.example/a/b/", "https://h.example:8443/a", "https://u:p@h.example/a",
])
def test_paths_that_would_alias_are_refused(url):
    assert prov.asset_repo_path(url) is None


def test_update_api_retries_then_succeeds_and_gives_up_clearly():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"url": "https://h/x", "version": "c" * 40,
                                         "sha256hash": "a" * 64})
    assert updates.latest(httpx.Client(transport=httpx.MockTransport(handler)), "x", "stable")
    always = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    with pytest.raises(RuntimeError, match="503"):
        updates.latest(always, "x", "stable")


def test_backoff_waits_are_reported():
    said = []
    up = FakeUpstream()
    up.status_queue = [429]
    mp.top_n(up.client(), 1, on_wait=said.append)
    assert said and "429" in said[0]


def test_a_selection_with_no_installable_build_is_an_error(up):
    src = _vs(kinds=["extensions"], platforms=["linux-x64"],
              extensions=config.ExtensionSelection(ids=["ms-python.vscode-pylance"]))
    r = get_provider("vscode").resolve(src)
    assert any("no installable" in e for e in r.errors)


def test_refetch_keeps_history_for_providers_that_carry_it(monkeypatch, tmp_path):
    from mmw.core import sync_engine
    captured = {}

    async def fake_download(**kw):
        captured["refetch"] = kw["refetch_metadata"]
        return downloader.MirrorSummary(mirror=kw["mirror_name"])
    monkeypatch.setattr(sync_engine, "download_mirror", fake_download)
    seen = []
    vs = get_provider("vscode")
    monkeypatch.setattr(type(vs), "fetch_all",
                        lambda self, m, c, status_callback=None, history=None:
                        (seen.append(history) or ([], [])))
    (unit,) = vs.expand(_vs(kinds=["server"], platforms=["linux-x64"], qualities=["stable"]))
    ex = concurrent.futures.ThreadPoolExecutor(1)
    try:
        with Pool(tmp_path) as pool:
            asyncio.run(sync_engine.sync_mirror(unit, pool=pool, mirror_dir=tmp_path,
                                                max_connections=1, executor=ex))
            captured["refetch"]()
    finally:
        ex.shutdown()
    assert len(seen) == 2 and seen[1] is not None


def test_unit_history_reads_refs_only_when_asked():
    from mmw.core.history import UnitHistory

    class Pool_:
        calls = 0

        def ref_map(self, name):
            Pool_.calls += 1
            return {}
    h = UnitHistory(Pool_(), "u")
    assert Pool_.calls == 0
    h.paths("")
    h.paths("")
    assert Pool_.calls == 1


def test_tui_sync_resolves_only_the_selected_sources(monkeypatch):
    from mmw.tui import repositories
    calls = []
    monkeypatch.setattr(prov.VscodeProvider, "resolve",
                        lambda self, s, progress=None: calls.append(s) or None)
    apt_src = config.SourceConfig(url=APT, suites=["noble"], components=["main"], arches=["amd64"])
    cfg = config.Config(settings=config.Settings(mirror_dir="/tmp/x"), sources=[apt_src, _vs()])
    repositories.resolve_selection(cfg, ([apt_src.identity], []))
    assert calls == []
