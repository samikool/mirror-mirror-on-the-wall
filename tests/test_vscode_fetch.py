"""fetch_all for each VS Code unit kind, and server-commit retention."""
import hashlib
import json

import pytest

from mmw.core import config
from mmw.core.history import UnitHistory
from mmw.providers import get_provider
from mmw.providers.vscode import marketplace as mp
from mmw.providers.vscode import provider as prov
from mmw.providers.vscode import updates
from vscode_fakes import FakeUpstream, load


@pytest.fixture
def up(monkeypatch):
    fake = FakeUpstream()
    monkeypatch.setattr(prov, "_client", fake.client)
    monkeypatch.setattr(mp, "BACKOFF", 0.0)
    monkeypatch.setattr(mp, "RATE_LIMIT_WAIT", 0.0)
    return fake


def _unit(kind, platform="linux-x64", quality=None):
    src = config.VscodeSource(provider="vscode", kinds=[kind], platforms=[platform],
                              qualities=[quality] if quality else [],
                              extensions=config.ExtensionSelection(ids=["ms-python.python"]))
    vs = get_provider("vscode")
    return src, next(u for u in vs.expand(src) if u.dims.get("platform") == platform
                     and (not quality or u.dims.get("quality") == quality))


def test_extensions_unit(up):
    src, unit = _unit("extensions")
    vs = get_provider("vscode")
    vs.resolve(src)
    pkgs, meta = vs.fetch_all(unit, up.client())
    assert pkgs and all(not p.checksum_known and p.checksum == "" for p in pkgs)
    assert all(p.repo_path.split("/", 1)[0].endswith(".vsassets.io") for p in pkgs)
    ext_meta = [m for m in meta if m.repo_path.startswith(".vscode-meta/extensions/linux-x64/")]
    assert ext_meta and all(m.expected_sha256 == hashlib.sha256(m.content).hexdigest()
                            for m in ext_meta)
    stored = json.loads(ext_meta[0].content)
    assert all(v.get("targetPlatform") in (None, "universal", "linux-x64")
               for v in stored["versions"])
    # The malicious list rides the universal unit only (once per source).
    assert not [m for m in meta if m.is_release]
    universal = next(u for u in vs.expand(src) if u.name == "vscode-extensions-universal")
    (release,) = [m for m in vs.fetch_all(universal, up.client())[1] if m.is_release]
    assert release.repo_path == "main.vscode-cdn.net/extensions/marketplace.json"


def test_fetching_an_unresolved_extensions_unit_says_so(up):
    _, unit = _unit("extensions")
    get_provider("vscode").catalog.pop(unit.source, None)
    with pytest.raises(RuntimeError, match="resolve"):
        get_provider("vscode").fetch_all(unit, up.client())


def test_desktop_unit_downloads_each_build_with_its_sha256(up):
    _, unit = _unit("desktop", quality="stable")
    pkgs, meta = get_provider("vscode").fetch_all(unit, up.client())
    fixture = load("update-linux-x64-stable.json")
    linux = next(p for p in pkgs if p.url == fixture["url"])
    from vscode_fakes import fake_asset_bytes
    # The fake answers with its stand-in's hash (see FakeUpstream).
    assert linux.checksum == hashlib.sha256(fake_asset_bytes(fixture["url"])).hexdigest()
    assert linux.checksum_known
    assert linux.repo_path == fixture["url"].split("://", 1)[1]
    assert updates.meta_path("linux-x64", "stable", fixture["version"]) in \
        {m.repo_path for m in meta}


def test_a_build_the_api_does_not_serve_is_skipped(up):
    up.update_overrides["linux-deb-x64"] = 204
    _, unit = _unit("desktop", quality="stable")
    pkgs, _ = get_provider("vscode").fetch_all(unit, up.client())
    assert not any("deb" in p.repo_path for p in pkgs)


class _History:
    def __init__(self, files: dict[str, bytes]):
        self.files = files

    def paths(self, prefix):
        return sorted(p for p in self.files if p.startswith(prefix))

    def read(self, path):
        return self.files.get(path)


def _update(commit, ts, ident="server-linux-x64"):
    return {"url": f"https://vscode.download.prss.microsoft.com/dbazure/download/stable/{commit}/"
                   f"vscode-{ident}.tar.gz",
            "name": "1.0", "version": commit, "productVersion": "1.0",
            "sha256hash": hashlib.sha256(commit.encode()).hexdigest(), "timestamp": ts}


def test_server_keeps_latest_and_two_previous_commits(up):
    old = {updates.meta_path("server-linux-x64", "stable", c): json.dumps(_update(c, ts)).encode()
           for c, ts in (("a" * 40, 1), ("b" * 40, 2), ("c" * 40, 3))}
    up.update_overrides["server-linux-x64"] = _update("d" * 40, 4)
    _, unit = _unit("server", quality="stable")
    pkgs, meta = get_provider("vscode").fetch_all(unit, up.client(), history=_History(old))
    kept = {m.repo_path.rsplit("/", 1)[1][:-5] for m in meta
            if m.repo_path.startswith(".vscode-meta/update/server-linux-x64/")}
    assert kept == {"d" * 40, "c" * 40, "b" * 40}
    assert {p.checksum for p in pkgs if "server-linux-x64" in p.repo_path} == \
        {hashlib.sha256(c.encode()).hexdigest() for c in ("d" * 40, "c" * 40, "b" * 40)}


def test_desktop_keeps_only_the_latest(up):
    old = {updates.meta_path("linux-x64", "stable", "a" * 40):
           json.dumps(_update("a" * 40, 1, "linux-x64")).encode()}
    _, unit = _unit("desktop", quality="stable")
    _, meta = get_provider("vscode").fetch_all(unit, up.client(), history=_History(old))
    assert not any("a" * 40 in m.repo_path for m in meta)


@pytest.mark.parametrize("url", [
    "https://x.gallerycdn.vsassets.io/a/../../etc/passwd",
    "https://x.gallerycdn.vsassets.io/a?sig=1",
    "https://x.gallerycdn.vsassets.io/a\\b",
    "http:///nohost",
])
def test_unsafe_asset_urls_are_refused(url):
    assert prov.asset_repo_path(url) is None


def test_unit_history_reads_a_units_previous_refs(tmp_path):
    from mmw.core.pool import KIND_META, Pool
    body = b'{"x": 1}'
    cs = hashlib.sha256(body).hexdigest()
    with Pool(tmp_path) as pool:
        pool.add(body, cs, "sha256", "x.json", len(body))
        pool.reconcile_refs("u", [(cs, ".vscode-meta/update/a/stable/c.json", KIND_META)])
    with Pool(tmp_path, read_only=True) as pool:
        h = UnitHistory(pool, "u")
        assert h.paths(".vscode-meta/update/") == [".vscode-meta/update/a/stable/c.json"]
        assert h.read(".vscode-meta/update/a/stable/c.json") == body
        assert h.read("nope") is None


def test_verify_witness_checks_every_named_asset(tmp_path):
    from mmw.core.pool import KIND_META, KIND_PKG, Pool
    ext = {"publisher": {"publisherName": "p"}, "extensionName": "e",
           "versions": [{"version": "1", "files": [
               {"assetType": "VSIX", "source": "https://p.gallerycdn.vsassets.io/x/VSIX"},
               {"assetType": "Icon", "source": "https://p.gallerycdn.vsassets.io/x/Icon"}]}]}
    meta = json.dumps(ext).encode()
    vsix = b"vsix"
    with Pool(tmp_path) as pool:
        for body, name in ((meta, "e.json"), (vsix, "VSIX")):
            pool.add(body, hashlib.sha256(body).hexdigest(), "sha256", name, len(body))
        pool.reconcile_refs("vscode-extensions-linux-x64", [
            (hashlib.sha256(meta).hexdigest(), ".vscode-meta/extensions/linux-x64/p.e.json", KIND_META),
            (hashlib.sha256(vsix).hexdigest(), "p.gallerycdn.vsassets.io/x/VSIX", KIND_PKG)])
    _, unit = _unit("extensions")
    with Pool(tmp_path, read_only=True) as pool:
        r = get_provider("vscode").verify_witness(unit, pool)
    assert (r.total, r.present, r.missing_count) == (2, 1, 1)


@pytest.mark.parametrize("asset_count", [1, 1000])
def test_verify_witness_batches_lookups_and_detects_missing_package_rows(tmp_path, asset_count):
    from mmw.core.pool import KIND_META, KIND_PKG, Pool
    _, unit = _unit("extensions")
    paths = [f"p.gallerycdn.vsassets.io/x/{i}" for i in range(asset_count)]
    meta = json.dumps({"versions": [{"files": [
        {"source": f"https://{p}"} for p in paths]}]}).encode()
    meta_cs = hashlib.sha256(meta).hexdigest()
    asset_cs = hashlib.sha256(b"asset").hexdigest()
    with Pool(tmp_path) as pool:
        pool.add(meta, meta_cs, "sha256", "e.json", len(meta))
        pool.add(b"asset", asset_cs, "sha256", "asset", 5)
        pool.reconcile_refs(unit.name, [
            (meta_cs, ".vscode-meta/extensions/linux-x64/p.e.json", KIND_META),
            *[(asset_cs, p, KIND_PKG) for p in paths[:-1]],
            ("f" * 64, paths[-1], KIND_PKG),
        ])
    with Pool(tmp_path, read_only=True) as pool:
        queries = []
        with pool._read() as conn:
            conn.set_trace_callback(queries.append)
        r = get_provider("vscode").verify_witness(unit, pool)
    assert (r.total, r.present, r.missing_count) == (asset_count, asset_count - 1, 1)
    assert len([q for q in queries if q.lstrip().upper().startswith("SELECT")]) == 2
    assert set(r.timings) == {"lookup", "metadata", "assets"}


@pytest.mark.parametrize("damage", ["missing-row", "missing-file", "invalid-json"])
def test_verify_witness_rejects_unreadable_metadata(tmp_path, damage):
    from mmw.core.pool import KIND_META, Pool
    _, unit = _unit("extensions")
    body = b"invalid" if damage == "invalid-json" else b'{"versions": []}'
    cs = hashlib.sha256(body).hexdigest()
    rel = ".vscode-meta/extensions/linux-x64/p.e.json"
    with Pool(tmp_path) as pool:
        if damage != "missing-row":
            path = pool.add(body, cs, "sha256", "e.json", len(body))
            if damage == "missing-file":
                path.unlink()
        pool.reconcile_refs(unit.name, [(cs, rel, KIND_META)])
    with Pool(tmp_path, read_only=True) as pool:
        r = get_provider("vscode").verify_witness(unit, pool)
    assert not r.complete
    assert r.error == f"unreadable {rel}"


def test_witness_and_catalog_read_the_metadata_with_reads_in_flight(tmp_path, monkeypatch):
    # 120k metadata JSONs one at a time is a seek each on a spinning disk.
    import threading
    import time
    from mmw.core import bulk_read
    from mmw.core.pool import KIND_META, Pool
    from mmw.providers.vscode import publish as vpub
    _, unit = _unit("extensions")
    with Pool(tmp_path) as pool:
        rows = []
        for i in range(8):
            body = json.dumps({"publisher": {"publisherName": "p"}, "extensionName": f"e{i}",
                               "versions": [{"version": "1", "files": []}]}).encode()
            cs = hashlib.sha256(body).hexdigest()
            pool.add(body, cs, "sha256", f"e{i}.json", len(body))
            rows.append((cs, f".vscode-meta/extensions/linux-x64/p.e{i}.json", KIND_META))
        pool.reconcile_refs(unit.name, rows)
    real, lock, live, peak = bulk_read._read_bytes, threading.Lock(), [0], [0]

    def tracked(path):
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        return real(path)
    monkeypatch.setattr(bulk_read, "_read_bytes", tracked)
    with Pool(tmp_path, read_only=True) as pool:
        r = get_provider("vscode").verify_witness(unit, pool)
        assert not r.error and peak[0] > 1
        peak[0] = 0
        assert len(vpub.catalog(pool, [unit])) == 8 and peak[0] > 1


def test_the_stable_desktop_build_carries_the_workbench_alerts(up):
    # VS Code fetches https://main.vscode-cdn.net/core/stable.json at startup.
    vs = get_provider("vscode")
    _, desktop = _unit("desktop", quality="stable")
    meta = vs.fetch_all(desktop, up.client())[1]
    assert [m.repo_path for m in meta if m.is_release] == ["main.vscode-cdn.net/core/stable.json"]
    _, server = _unit("server", quality="stable")
    assert not [m for m in vs.fetch_all(server, up.client())[1] if m.is_release]
