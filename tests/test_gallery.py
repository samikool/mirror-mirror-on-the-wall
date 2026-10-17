"""The far-side gallery, served for real on a loopback port."""
import json
import os
import threading
import time

import httpx
import pytest

from mmw.resources import gallery


def _ext(pub, name, installs=0, display=None, short="", versions=None):
    return {"publisher": {"publisherName": pub}, "extensionName": name,
            "extensionId": f"id-{pub}-{name}", "displayName": display or name,
            "shortDescription": short, "lastUpdated": "2026-01-01T00:00:00.000Z",
            "publishedDate": "2020-01-01T00:00:00.000Z",
            "statistics": [{"statisticName": "install", "value": installs}],
            "versions": versions or [{"version": "1.0.0", "files": [
                {"assetType": "Microsoft.VisualStudio.Services.VSIXPackage",
                 "source": f"https://{pub}.gallerycdn.vsassets.io/x/{name}/1.0.0/VSIX"}]}]}


def _build(commit, ident="server-linux-x64"):
    return {"url": f"https://vscode.download.prss.microsoft.com/dbazure/download/stable/{commit}/{ident}.tar.gz",
            "version": commit, "sha256hash": "a" * 64, "timestamp": 1}


@pytest.fixture
def served(tmp_path):
    (tmp_path / "catalog.json").write_text(json.dumps([
        _ext("golang", "go", 50, "Go", "Rich Go language support"),
        _ext("ms-python", "python", 900, "Python", "Python language support"),
        _ext("redhat", "vscode-yaml", 300, "YAML")]))
    (tmp_path / "updates.json").write_text(json.dumps({"server-linux-x64": {"stable": {
        "latest": "b" * 40, "builds": {"a" * 40: _build("a" * 40), "b" * 40: _build("b" * 40)}}}}))
    store = gallery.Store(str(tmp_path))
    server = gallery.make_server(store, "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with httpx.Client(base_url=f"http://127.0.0.1:{server.server_address[1]}") as client:
        yield client, tmp_path
    server.shutdown()
    server.server_close()


def _q(*criteria, sort_by=0, page=1, size=50):
    return {"filters": [{"criteria": [{"filterType": 8, "value": "Microsoft.VisualStudio.Code"},
                                      {"filterType": 12, "value": "4096"}, *criteria],
                         "pageNumber": page, "pageSize": size, "sortBy": sort_by, "sortOrder": 0}],
            "flags": 914}


def _names(resp):
    return [e["extensionName"] for e in resp.json()["results"][0]["extensions"]]


def test_empty_search_lists_everything_by_installs(served):
    c, _ = served
    r = c.post("/_apis/public/gallery/extensionquery?api-version=3.0-preview.1", json=_q())
    assert r.status_code == 200 and _names(r) == ["python", "vscode-yaml", "go"]
    meta = r.json()["results"][0]["resultMetadata"][0]
    assert meta["metadataItems"][0]["count"] == 3


@pytest.mark.parametrize("crit,want", [
    ({"filterType": 7, "value": "MS-Python.Python"}, ["python"]),
    ({"filterType": 4, "value": "id-golang-go"}, ["go"]),
    ({"filterType": 10, "value": "language"}, ["python", "go"]),
    ({"filterType": 10, "value": "yaml"}, ["vscode-yaml"]),
    ({"filterType": 10, "value": "nothing-matches"}, []),
    ({"filterType": 5, "value": "Themes"}, []),
])
def test_filters(served, crit, want):
    c, _ = served
    assert _names(c.post("/_apis/public/gallery/extensionquery", json=_q(crit))) == want


def test_sort_by_name_and_paging(served):
    c, _ = served
    assert _names(c.post("/_apis/public/gallery/extensionquery", json=_q(sort_by=2))) == \
        ["go", "python", "vscode-yaml"]
    r = c.post("/_apis/public/gallery/extensionquery", json=_q(page=2, size=2))
    assert _names(r) == ["go"]


def test_malformed_query_is_400(served):
    c, _ = served
    assert c.post("/_apis/public/gallery/extensionquery", content=b"{not json").status_code == 400
    assert c.post("/_apis/public/gallery/extensionquery", json={"nope": 1}).status_code == 400


def test_update_check(served):
    c, _ = served
    assert c.get(f"/api/update/server-linux-x64/stable/{'b' * 40}").status_code == 204
    r = c.get(f"/api/update/server-linux-x64/stable/{'a' * 40}")
    assert r.status_code == 200 and r.json()["version"] == "b" * 40
    assert c.get(f"/api/update/darwin/stable/{'a' * 40}").status_code == 404


def test_commit_redirects_to_a_retained_build(served):
    c, _ = served
    r = c.get(f"/commit:{'a' * 40}/server-linux-x64/stable")
    assert r.status_code == 302 and r.headers["location"].endswith(f"/{'a' * 40}/server-linux-x64.tar.gz")
    assert c.get(f"/commit:{'c' * 40}/server-linux-x64/stable").status_code == 404


def test_fallback_asset_url_redirects_to_the_stored_file(served):
    c, _ = served
    r = c.get("/_apis/public/gallery/publisher/golang/extension/go/1.0.0/assetbyname/"
              "Microsoft.VisualStudio.Services.VSIXPackage")
    assert r.status_code == 302 and r.headers["location"] == \
        "https://golang.gallerycdn.vsassets.io/x/go/1.0.0/VSIX"
    assert c.get("/_apis/public/gallery/publisher/golang/extension/go/9.9.9/assetbyname/X").status_code == 404


def test_healthz_and_unknown_paths(served):
    c, _ = served
    assert c.get("/healthz").status_code == 200
    assert c.get("/nope").status_code == 404


def test_catalog_reloads_when_it_changes(served):
    c, data = served
    time.sleep(0.02)
    (data / "catalog.json").write_text(json.dumps([_ext("new", "thing")]))
    import os
    os.utime(data / "catalog.json", (time.time() + 5, time.time() + 5))
    assert _names(c.post("/_apis/public/gallery/extensionquery", json=_q())) == ["thing"]


def test_an_unreadable_catalog_keeps_the_last_good_one(served):
    c, data = served
    (data / "catalog.json").write_text("{broken")
    import os
    os.utime(data / "catalog.json", (time.time() + 9, time.time() + 9))
    assert len(_names(c.post("/_apis/public/gallery/extensionquery", json=_q()))) == 3


def _platform_versions(name):
    return [{"version": "2.0.0", "targetPlatform": tp, "files": [
        {"assetType": "Microsoft.VisualStudio.Services.VSIXPackage",
         "source": f"https://ms-python.gallerycdn.vsassets.io/x/{name}/2.0.0/{tp}/VSIX"}]}
        for tp in ("win32-x64", "linux-x64", "darwin-arm64")]


def test_the_fallback_asset_url_honours_target_platform(served):
    """VS Code always downloads a VSIX through the fallback URL with
    `targetPlatform=`; ignoring it handed every client the first platform's build."""
    c, data = served
    universal = {"version": "2.0.0", "files": [
        {"assetType": "Microsoft.VisualStudio.Services.VSIXPackage",
         "source": "https://ms-python.gallerycdn.vsassets.io/x/debugpy/2.0.0/VSIX"}]}
    (data / "catalog.json").write_text(json.dumps([
        _ext("ms-python", "python", versions=_platform_versions("python")),
        _ext("ms-python", "debugpy", versions=[*_platform_versions("debugpy")[:1], universal])]))
    os.utime(data / "catalog.json", (time.time() + 5, time.time() + 5))
    base = "/_apis/public/gallery/publisher/ms-python/extension/{}/2.0.0/assetbyname/" \
           "Microsoft.VisualStudio.Services.VSIXPackage?redirect=true"
    for tp in ("linux-x64", "darwin-arm64", "win32-x64"):
        r = c.get(base.format("python") + f"&targetPlatform={tp}")
        assert r.status_code == 302 and f"/{tp}/VSIX" in r.headers["location"], tp
    # no build for that platform: 404, not some other platform's VSIX
    assert c.get(base.format("python") + "&targetPlatform=alpine-arm64").status_code == 404
    # a universal build serves any platform; no parameter prefers it
    for q in ("&targetPlatform=linux-x64", ""):
        assert c.get(base.format("debugpy") + q).headers["location"].endswith("/debugpy/2.0.0/VSIX")


def test_head_answers_like_get(served):
    c, _ = served
    assert c.head(f"/api/update/server-linux-x64/stable/{'b' * 40}").status_code == 204
    assert c.head(f"/api/update/server-linux-x64/stable/{'a' * 40}").status_code == 200
    assert c.head("/healthz").status_code == 200


@pytest.mark.parametrize("body", [
    b'{"filters":[{"pageSize":1e999}]}',
    b"[" * 100000 + b"]" * 100000,
])
def test_hostile_queries_are_400(served, body):
    c, _ = served
    assert c.post("/_apis/public/gallery/extensionquery", content=body).status_code == 400


def test_a_negative_content_length_is_400_not_a_hang(served):
    import socket
    c, _ = served
    port = int(str(c.base_url).rsplit(":", 1)[1].strip("/"))
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b"POST /_apis/public/gallery/extensionquery HTTP/1.1\r\nHost: x\r\n"
                  b"Content-Length: -1\r\n\r\n")
        assert s.recv(64).startswith(b"HTTP/1.0 400")


def test_healthz_fails_when_the_data_files_are_missing(tmp_path):
    store = gallery.Store(str(tmp_path))
    assert store.healthy is False


def test_the_workbench_preflight_for_a_search_is_allowed(served):
    # VS Code's workbench queries from its own origin, so the browser engine
    # asks first; a 501 here leaves the Extensions view empty.
    c, _ = served
    r = c.options("/_apis/public/gallery/extensionquery", headers={
        "Origin": "vscode-file://vscode-app",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-market-client-id"})
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == "*"
    assert "POST" in r.headers["access-control-allow-methods"]
    assert r.headers["access-control-allow-headers"] == "content-type,x-market-client-id"
    answer = c.post("/_apis/public/gallery/extensionquery", json=_q(),
                    headers={"Origin": "vscode-file://vscode-app"})
    assert answer.status_code == 200
    assert answer.headers["access-control-allow-origin"] == "*"


def test_latest_and_sha_download_links_redirect_to_the_newest_build(served):
    # Microsoft's direct-download links, which scripts and bookmarks use.
    c, _ = served
    for path in ("/latest/server-linux-x64/stable",
                 "/sha/download?build=stable&os=server-linux-x64"):
        r = c.get(path)
        assert r.status_code == 302, path
        assert r.headers["location"].endswith(f"/{'b' * 40}/server-linux-x64.tar.gz")
    assert c.get("/latest/darwin/stable").status_code == 404
    assert c.get("/sha/download?build=stable&os=darwin").status_code == 404
    assert c.get("/sha/download").status_code == 404


def test_the_download_page_links_every_identity_s_newest_build(served):
    # Linux's "Download Update" opens https://code.visualstudio.com.
    c, _ = served
    for path in ("/", "/Download", "/download"):
        r = c.get(path, headers={"Host": "code.visualstudio.com"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
        assert f"/{'b' * 40}/server-linux-x64.tar.gz" in r.text
        assert f"/{'a' * 40}/" not in r.text          # only the newest build
