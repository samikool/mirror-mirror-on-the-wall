"""The recorded fixtures have the shapes the provider depends on."""
from vscode_fakes import load


def test_gallery_fixtures_have_extensions_with_files_and_properties():
    for name in ("top3.json", "names-python.json", "names-deps.json"):
        exts = load(name)["results"][0]["extensions"]
        assert exts, name
        for ext in exts:
            assert ext["publisher"]["publisherName"] and ext["extensionName"]
            v = ext["versions"][0]
            assert v["version"] and v["files"]
            assert all(f["source"].startswith("https://") for f in v["files"])


def test_missing_name_returns_no_extensions():
    assert load("names-missing.json")["results"][0]["extensions"] == []


def test_update_fixtures_name_a_build():
    for ident in ("linux-x64", "server-linux-x64", "cli-alpine-x64"):
        u = load(f"update-{ident}-stable.json")
        assert u["url"].startswith("https://") and len(u["version"]) == 40
        assert len(u["sha256hash"]) == 64


def test_malicious_list_shape():
    assert isinstance(load("malicious.json")["malicious"], list)
