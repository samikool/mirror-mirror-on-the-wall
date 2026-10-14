"""Gallery queries, paging, backoff, and choosing versions — against the
recorded fixtures through a fake transport."""
import json

import pytest

from mmw.providers.vscode import marketplace as mp
from vscode_fakes import FakeUpstream, load


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(mp, "BACKOFF", 0.0)
    monkeypatch.setattr(mp, "RATE_LIMIT_WAIT", 0.0)


def test_query_sends_what_vscodeoffline_sends():
    up = FakeUpstream()
    mp.query(up.client(), [{"filterType": 10, "value": ""}], page_size=3, sort_by=4, sort_order=2)
    req = up.requests[0]
    assert str(req.url) == mp.GALLERY_URL
    assert req.headers["accept"] == "application/json;api-version=3.0-preview.1"
    body = json.loads(req.content)
    crit = body["filters"][0]["criteria"]
    assert crit[:2] == [{"filterType": 8, "value": "Microsoft.VisualStudio.Code"},
                        {"filterType": 12, "value": "4096"}]
    assert body["flags"] == 914 and body["filters"][0]["sortBy"] == 4


def test_top_n():
    exts = mp.top_n(FakeUpstream().client(), 2)
    assert [mp.ext_id(e) for e in exts] == \
        [mp.ext_id(e) for e in load("top3.json")["results"][0]["extensions"][:2]]


def test_by_names_reports_only_what_exists():
    found = mp.by_names(FakeUpstream().client(), ["ms-python.python", "nobody.no-such-extension"])
    assert set(found) == {"ms-python.python"}


def test_429_backs_off_then_succeeds():
    up = FakeUpstream()
    up.status_queue = [429, 429]
    assert mp.top_n(up.client(), 1)
    assert len(up.requests) == 3


def test_persistent_429_is_a_clear_error_not_a_hang():
    up = FakeUpstream()
    up.status_queue = [429] * 50
    with pytest.raises(mp.MarketplaceError, match="429"):
        mp.top_n(up.client(), 1)
    assert len(up.requests) == 6


def _v(version, platform=None, pre=False, deps=""):
    v = {"version": version, "files": [], "properties": []}
    if platform:
        v["targetPlatform"] = platform
    if pre:
        v["properties"].append({"key": mp.PRERELEASE_KEY, "value": "true"})
    if deps:
        v["properties"].append({"key": mp.DEPENDENCIES_KEY, "value": deps})
    return v


def test_pick_prefers_the_platform_build_and_skips_prereleases():
    ext = {"versions": [_v("2.0.0", "linux-x64", pre=True), _v("1.9.0", "linux-x64"),
                        _v("1.9.0", "win32-x64"), _v("1.9.0")]}
    assert mp.pick_versions(ext, "linux-x64", prerelease=False) == [ext["versions"][1]]
    assert mp.pick_versions(ext, "darwin-arm64", prerelease=False) == [ext["versions"][3]]
    assert mp.pick_versions(ext, "linux-x64", prerelease=True) == [ext["versions"][0]]


def test_pick_returns_nothing_without_a_build_for_the_platform():
    ext = {"versions": [_v("1.0.0", "win32-x64")]}
    assert mp.pick_versions(ext, "linux-x64", prerelease=False) == []


def test_dependencies_come_from_the_chosen_release():
    ext = {"versions": [_v("2.0.0", pre=True, deps="a.pre"), _v("1.0.0", deps="A.One, b.two")]}
    assert mp.dependencies(ext, prerelease=False) == {"a.one", "b.two"}


def test_a_latest_only_answer_can_hold_no_release_at_all():
    """Recorded reality: ms-python.python's latest builds are all prereleases,
    so its release (and that release's pack members) only appear when every
    version is asked for — why the provider re-queries."""
    latest = load("names-python.json")["results"][0]["extensions"][0]
    assert mp.pick_versions(latest, "linux-x64", prerelease=False) == []
    assert mp.dependencies(latest, prerelease=False) == set()


def test_recorded_python_dependencies_are_found():
    full = load("names-python-all.json")["results"][0]["extensions"][0]
    assert mp.pick_versions(full, "linux-x64", prerelease=False)
    deps = mp.dependencies(full, prerelease=False)
    recorded = {mp.ext_id(e) for e in load("names-deps.json")["results"][0]["extensions"]}
    assert deps                      # the recorded release declares pack members
    assert recorded and recorded <= deps | mp.dependencies(
        load("names-python.json")["results"][0]["extensions"][0], prerelease=True)


def test_transient_403_is_retried_without_dropping_extensions():
    up = FakeUpstream()
    up.status_queue = [403]
    phases = []
    assert mp.search_all(up.client(), on_wait=phases.append)
    assert any('HTTP 403' in phase for phase in phases)
    assert any('listed' in phase for phase in phases)


def test_persistent_403_still_fails_with_bounded_retries():
    up = FakeUpstream()
    up.status_queue = [403] * 50
    with pytest.raises(mp.MarketplaceError, match='403'):
        mp.search_all(up.client())
    assert len(up.requests) == mp.ATTEMPTS


def test_rate_limit_respects_retry_after(monkeypatch):
    import httpx
    waits = []
    monkeypatch.setattr(mp.time, 'sleep', waits.append)
    up = FakeUpstream()
    calls = []
    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={'Retry-After': '196'})
        return up(request)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert mp.top_n(client, 1)
    assert waits == [196]


def test_all_pages_in_publication_order_while_top_n_uses_install_counts(monkeypatch):
    import httpx
    monkeypatch.setattr(mp, 'PAGE', 1)
    extensions = load('top3.json')['results'][0]['extensions']
    queries = []
    def respond(request):
        query = json.loads(request.content)['filters'][0]
        queries.append(query)
        offset = (query['pageNumber'] - 1) * query['pageSize']
        return httpx.Response(200, json={'results': [{
            'extensions': extensions[offset:offset + query['pageSize']],
            'resultMetadata': [{'metadataType': 'ResultCount',
                                'metadataItems': [{'count': len(extensions)}]}]}]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        assert mp.search_all(client) == extensions
        assert [q['pageNumber'] for q in queries] == [1, 2, 3]
        assert all((q['sortBy'], q['sortOrder']) == (10, 1) for q in queries)
        queries.clear()
        assert mp.top_n(client, 1) == extensions[:1]
        assert (queries[0]['sortBy'], queries[0]['sortOrder']) == (4, 2)
