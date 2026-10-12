"""VscodeProvider: units, presentation, and resolving the extension selection."""
import pytest

from mmw.core import config
from mmw.providers import get_provider
from mmw.providers.vscode import marketplace as mp
from mmw.providers.vscode import provider as prov
from vscode_fakes import FakeUpstream, load


@pytest.fixture
def up(monkeypatch):
    fake = FakeUpstream()
    monkeypatch.setattr(prov, "_client", fake.client)
    monkeypatch.setattr(mp, "BACKOFF", 0.0)
    return fake


def _src(**kw):
    return config.VscodeSource(provider="vscode", **kw)


def test_expand_crosses_kinds_platforms_qualities():
    vs = get_provider("vscode")
    units = vs.expand(_src(platforms=["linux-x64", "web"], qualities=["stable"]))
    assert sorted(u.name for u in units) == [
        "vscode-desktop-linux-x64-stable", "vscode-extensions-linux-x64",
        "vscode-extensions-universal", "vscode-extensions-web",
        "vscode-server-linux-x64-stable"]
    ext = next(u for u in units if u.name == "vscode-extensions-linux-x64")
    assert ext.dims == {"kind": "extensions", "platform": "linux-x64"}
    assert ext.source == "https://marketplace.visualstudio.com" and ext.provider == "vscode"


def test_empty_means_all():
    units = get_provider("vscode").expand(_src())
    assert len([u for u in units if u.dims["kind"] == "extensions"]) == 11   # 10 + universal
    assert {u.dims.get("quality") for u in units if u.dims["kind"] != "extensions"} == \
        {"stable", "insider"}


def test_presentation():
    vs = get_provider("vscode")
    src = _src(kinds=["extensions"], extensions=config.ExtensionSelection(top_n=5, ids=["a.b"]))
    d = vs.describe(src)
    assert ("Extensions", "top 5 + 1 listed") in d.fields and d.location_label == "Upstream"
    (u,) = vs.expand(_src(kinds=["server"], platforms=["linux-x64"], qualities=["stable"]))
    assert vs.unit_label(u) == ("server · linux-x64 · stable", "")
    assert vs.publishes is False and vs.by_hash is False


def test_resolve_unions_selections_and_follows_dependencies(up):
    top = [mp.ext_id(e) for e in load("top3.json")["results"][0]["extensions"][:2]]
    src = _src(kinds=["extensions"], platforms=["linux-x64"],
               extensions=config.ExtensionSelection(top_n=2, ids=["ms-python.python"]))
    r = get_provider("vscode").resolve(src)
    assert not r.errors
    catalog = get_provider("vscode").catalog[src.identity]
    py = load("names-python.json")["results"][0]["extensions"][0]
    deps = mp.dependencies(py, prerelease=False)
    assert set(top) | {"ms-python.python"} | (deps & set(up.catalog)) <= set(catalog)
    assert any("dependencies" in n for n in r.notes)


def test_a_missing_id_is_reported_not_fatal(up):
    src = _src(kinds=["extensions"], platforms=["linux-x64"],
               extensions=config.ExtensionSelection(ids=["ms-python.python", "nobody.nothing"]))
    r = get_provider("vscode").resolve(src)
    assert not r.errors and any("nobody.nothing" in n for n in r.notes)


def test_a_selection_that_resolves_to_nothing_is_an_error(up):
    src = _src(kinds=["extensions"], extensions=config.ExtensionSelection(ids=["nobody.nothing"]))
    assert get_provider("vscode").resolve(src).errors


def test_no_extension_selection_at_all_is_fine_for_builds(up):
    r = get_provider("vscode").resolve(_src(kinds=["server"]))
    assert not r.errors and r.mirrors
