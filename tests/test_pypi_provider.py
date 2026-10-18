"""PypiProvider: units, resolve (index + closure), fetch_all with history."""
import gzip
import json
import threading

import pytest

from mmw.core import config
from mmw.providers import get_provider
from mmw.providers.base import ResolveContext
from mmw.providers.pypi import index
from mmw.providers.pypi.history import HISTORY_PATH
from pypi_fakes import FakePyPI, file_entry, metadata_text

GLIBC = "cp312-cp312-manylinux_2_17_x86_64"


def whl(name, version, tag="py3-none-any", requires=()):
    return file_entry(f"{name}-{version}-{tag}.whl", metadata=metadata_text(name, version, requires))


@pytest.fixture
def up(monkeypatch):
    fake = FakePyPI()
    fake.add("app", [whl("app", "1.0", requires=["lib<2", 'winonly; sys_platform == "win32"'])])
    fake.add("lib", [whl("lib", v, t) for v in ("1.9", "2.0") for t in (GLIBC, "cp312-cp312-win_amd64")])
    fake.add("winonly", [whl("winonly", "1.0", "cp312-cp312-win_amd64")])
    fake.add("tool", [file_entry("tool-0.1.tar.gz")])
    monkeypatch.setattr(index, "_client", fake.client)
    return fake


def _src(**kw):
    kw = {"pythons": ["3.12"], "platforms": ["linux-glibc", "windows"], "arches": ["amd64"], "all": True, **kw}
    return config.PypiSource(provider="pypi", **kw)


def _ctx(tmp_path, **kw):
    return ResolveContext(mirror_dir=tmp_path, max_connections=2, **kw)


class _History:
    def __init__(self, raw=None, checksums=None):
        self.raw = raw
        self.checksums = checksums or {}

    def read(self, path):
        return self.raw if path == HISTORY_PATH else None

    def checksum(self, path):
        return self.checksums.get(path)


def _fetch(prov, unit, history=None):
    return prov.fetch_all(unit, None, history=history or _History())


def test_expand_units_and_dims():
    units = get_provider("pypi").expand(_src())
    assert [u.name for u in units] == ["pypi-all-universal", "pypi-all-3.12-linux-glibc-amd64",
                                       "pypi-all-3.12-windows-amd64"]
    assert units[0].dims == {} and units[1].dims == {"python": "3.12", "platform": "linux-glibc", "arch": "amd64"}
    assert all(u.provider == "pypi" and u.source == "https://pypi.org/simple" for u in units)


def test_expand_nothing_selected(tmp_path, up):
    prov = get_provider("pypi")
    assert prov.expand(_src(all=False)) == []
    r = prov.resolve(_src(all=False), context=_ctx(tmp_path))
    assert r.mirrors == [] and any("nothing selected" in n for n in r.notes)


def test_resolve_then_fetch_all(tmp_path, up):
    prov = get_provider("pypi")
    r = prov.resolve(_src(), context=_ctx(tmp_path))
    assert not r.errors and len(r.mirrors) == 3
    assert any("4 projects" in n for n in r.notes)
    uni, linux, win = r.mirrors
    pkgs, meta = _fetch(prov, uni)
    assert sorted(p.filename for p in pkgs) == ["app-1.0-py3-none-any.whl"]
    p = pkgs[0]
    assert p.repo_path.startswith("files.pythonhosted.org/packages/") and p.checksum_known
    assert p.item_id == "app==1.0" and p.size > 0 and len(p.checksum) == 64
    paths = {m.repo_path: m for m in meta}
    assert paths[p.repo_path + ".metadata"].expected_sha256 and paths[p.repo_path + ".metadata"].content is None
    assert ".pypi-meta/report.json" in paths and HISTORY_PATH in paths
    lin = sorted(p.filename for p in _fetch(prov, linux)[0])
    assert lin == [f"lib-1.9-{GLIBC}.whl", f"lib-2.0-{GLIBC}.whl"]
    assert "winonly-1.0-cp312-cp312-win_amd64.whl" in [p.filename for p in _fetch(prov, win)[0]]


def test_report_lands_per_unit(tmp_path, up):
    prov = get_provider("pypi")
    r = prov.resolve(_src(), context=_ctx(tmp_path))
    meta = {m.repo_path: m for m in _fetch(prov, r.mirrors[1])[1]}
    report = json.loads(meta[".pypi-meta/report.json"].content)
    assert set(report) >= {"unsatisfiable", "unknown_deps", "sdist_fallback", "extras_gaps", "invalid"}


def test_resolve_stopped_is_an_error_and_resumable(tmp_path, up):
    stop = threading.Event()
    stop.set()
    prov = get_provider("pypi")
    r = prov.resolve(_src(), context=_ctx(tmp_path, stop_event=stop))
    assert r.mirrors == [] and any("stopped" in e for e in r.errors)
    assert not prov.resolve(_src(), context=_ctx(tmp_path)).errors


def test_fetch_all_before_resolve_raises(up):
    prov = type(get_provider("pypi"))()
    with pytest.raises(RuntimeError, match="not resolved"):
        prov.fetch_all(prov.expand(_src())[1], None, history=_History())


def test_history_carries_left_releases(tmp_path, up):
    prov = get_provider("pypi")
    linux = prov.resolve(_src(), context=_ctx(tmp_path)).mirrors[1]
    first = {m.repo_path: m for m in _fetch(prov, linux)[1]}[HISTORY_PATH].content
    up.projects["lib"]["files"].append(whl("lib", "1.9.5", GLIBC))
    up.projects["lib"]["serial"] = 2
    linux = prov.resolve(_src(), context=_ctx(tmp_path)).mirrors[1]
    pkgs, meta = _fetch(prov, linux, _History(first))
    names = sorted(p.filename for p in pkgs)
    assert f"lib-1.9.5-{GLIBC}.whl" in names and f"lib-1.9-{GLIBC}.whl" in names
    rows = json.loads(gzip.decompress({m.repo_path: m for m in meta}[HISTORY_PATH].content))
    left = [r for r in rows if r["left_at"]]
    assert [r["filename"] for r in left] == [f"lib-1.9-{GLIBC}.whl"]


def test_to_toml_roundtrip(tmp_path):
    src = _src(glibc="2.28", sdists=True)
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"), sources=[src])
    config.save(cfg, tmp_path)
    text = (tmp_path / "mmw.toml").read_text()
    assert 'provider = "pypi"' in text and "musl" not in text and "url" not in text
    (loaded,) = config.load(tmp_path).sources
    assert loaded == src


def test_registered_and_describes():
    prov = get_provider("pypi")
    d = prov.describe(_src())
    assert [label for label, _ in d.fields] == ["Provider", "Pythons", "Platforms", "Arches", "OS floors", "Requirements"]
    assert d.flags == ["all"] and d.location == "https://pypi.org/simple/"
    assert prov.unit_label(prov.expand(_src())[1]) == ("3.12 · linux-glibc · amd64", "")


def test_every_kept_wheel_gets_lazy_core_metadata(tmp_path, up):
    # A newer-glibc variant is kept but never chosen at the floor; its metadata differs.
    up.projects["lib"]["files"].append(
        file_entry("lib-2.0-cp312-cp312-manylinux_2_34_x86_64.whl", metadata=b"Name: lib\nVersion: 2.0\n"))
    up.projects["lib"]["serial"] = 2
    prov = get_provider("pypi")
    linux = prov.resolve(_src(), context=_ctx(tmp_path)).mirrors[1]
    pkgs, meta = _fetch(prov, linux)
    by_path = {m.repo_path: m for m in meta}
    variant = next(p for p in pkgs if "manylinux_2_34" in p.filename)
    entry = by_path[variant.repo_path + ".metadata"]
    assert entry.content is None and entry.url == variant.url + ".metadata"
    assert entry.expected_sha256 == up.projects["lib"]["files"][-1]["core-metadata"]["sha256"]
    assert all(m.content is None for m in meta if m.repo_path.endswith(".metadata"))


def test_retained_wheel_metadata_comes_from_its_ref(tmp_path, up):
    prov = get_provider("pypi")
    linux = prov.resolve(_src(), context=_ctx(tmp_path)).mirrors[1]
    pkgs, meta = _fetch(prov, linux)
    first = {m.repo_path: m for m in meta}[HISTORY_PATH].content
    old = next(p for p in pkgs if p.filename.startswith("lib-1.9-"))
    up.projects["lib"]["files"].append(whl("lib", "1.9.5", GLIBC))
    up.projects["lib"]["serial"] = 2
    linux = prov.resolve(_src(), context=_ctx(tmp_path)).mirrors[1]
    _, meta = _fetch(prov, linux, _History(first, {old.repo_path + ".metadata": "ab" * 32}))
    entry = {m.repo_path: m for m in meta}[old.repo_path + ".metadata"]
    assert entry.expected_sha256 == "ab" * 32 and entry.content is None


# ── the requirements flow (stage 2) ──────────────────────────────────────────

@pytest.fixture
def req_index(monkeypatch):
    from mmw.providers.pypi import uv as uvmod
    from pypi_server import serve
    fake = FakePyPI()
    fake.add("a", [whl("a", "1.0", requires=["b<2"])])
    fake.add("b", [whl("b", "1.0"), whl("b", "2.0")])
    fake.add("x", [whl("x", "1.0", requires=["b>=2"])])
    monkeypatch.setattr(index, "_client", fake.client)
    with serve(fake) as base:
        monkeypatch.setattr(uvmod, "INDEX_URL", f"{base}/simple")
        yield fake


def _req_src(reqs, **kw):
    kw = {"pythons": ["3.12"], "platforms": ["linux-glibc", "windows"], "arches": ["amd64"],
          "requirements": reqs, **kw}
    return config.PypiSource(provider="pypi", **kw)


def test_expand_req_units():
    units = get_provider("pypi").expand(_req_src(["a"], platforms=["linux-glibc"]))
    assert [u.name for u in units] == ["pypi-req-universal", "pypi-req-3.12-linux-glibc-amd64"]
    assert all(u.options["flow"] == "req" for u in units)


def test_resolve_req_only_fetches_only_pinned_pages(tmp_path, req_index):
    r = get_provider("pypi").resolve(_req_src(["a"]), context=_ctx(tmp_path))
    assert not r.errors, r.errors
    paths = [str(q.url) for q in req_index.requests]
    assert "https://pypi.org/simple/" not in paths
    assert set(req_index.project_requests()) <= {"a", "b"}


def test_req_fetch_all_serves_the_solution(tmp_path, req_index):
    prov = get_provider("pypi")
    r = prov.resolve(_req_src(["a", "b"]), context=_ctx(tmp_path))
    uni = next(u for u in r.mirrors if u.name == "pypi-req-universal")
    names = sorted(p.filename for p in _fetch(prov, uni)[0])
    assert names == ["a-1.0-py3-none-any.whl", "b-1.0-py3-none-any.whl"]


def test_failed_target_keeps_previous_state(tmp_path, req_index):
    prov = get_provider("pypi")
    r = prov.resolve(_req_src(["a", 'x; sys_platform == "win32"']), context=_ctx(tmp_path))
    assert "pypi-req-3.12-windows-amd64" not in [u.name for u in r.mirrors]   # keeps its previous state
    assert any("3.12-windows-amd64" in e for e in r.errors)


def test_both_flows_together(tmp_path, req_index):
    prov = get_provider("pypi")
    r = prov.resolve(_req_src(["a"], all=True), context=_ctx(tmp_path))
    assert not r.errors, r.errors
    names = [u.name for u in r.mirrors]
    assert names[0] == "pypi-all-universal" and "pypi-req-universal" in names
    all_files = {p.repo_path for p in _fetch(prov, r.mirrors[0])[0]}
    req_files = {p.repo_path for p in _fetch(prov, next(u for u in r.mirrors if u.name == "pypi-req-universal"))[0]}
    assert "a-1.0-py3-none-any.whl" in {p.rsplit("/", 1)[1] for p in all_files & req_files}


def test_missing_requirements_file_is_a_resolve_error(tmp_path, req_index):
    r = get_provider("pypi").resolve(_req_src([], requirements_file=str(tmp_path / "nope.txt")),
                                     context=_ctx(tmp_path))
    assert r.mirrors == [] and any("nope.txt" in e for e in r.errors)


def test_partial_failure_still_ships_req_universal(tmp_path, req_index):
    prov = get_provider("pypi")
    r = prov.resolve(_req_src(["a", 'x; sys_platform == "win32"']), context=_ctx(tmp_path))
    names = [u.name for u in r.mirrors]
    assert names == ["pypi-req-universal", "pypi-req-3.12-linux-glibc-amd64"]
    uni = r.mirrors[0]
    assert sorted(p.filename for p in _fetch(prov, uni)[0]) == ["a-1.0-py3-none-any.whl", "b-1.0-py3-none-any.whl"]


def test_partial_failure_keeps_what_the_failed_target_had(tmp_path, req_index):
    req_index.add("y", [whl("y", "1.0")])
    prov = get_provider("pypi")
    good = prov.resolve(_req_src(["a", 'y; sys_platform == "win32"']), context=_ctx(tmp_path))
    uni = good.mirrors[0]
    _, meta = _fetch(prov, uni)
    first = {m.repo_path: m for m in meta}[HISTORY_PATH].content
    bad = prov.resolve(_req_src(["a", 'y; sys_platform == "win32"', 'x; sys_platform == "win32"']),
                       context=_ctx(tmp_path))
    uni = bad.mirrors[0]
    assert uni.name == "pypi-req-universal" and any("windows" in e for e in bad.errors)
    pkgs, meta = _fetch(prov, uni, _History(first))
    assert "y-1.0-py3-none-any.whl" in [p.filename for p in pkgs]
    rows = json.loads(gzip.decompress({m.repo_path: m for m in meta}[HISTORY_PATH].content))
    assert all(r["left_at"] is None for r in rows if r["filename"].startswith("y-"))


def test_requirements_file_is_relative_to_the_contexts_config_dir(tmp_path, req_index):
    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "reqs.txt").write_text("a\n")
    src = _req_src([], requirements_file="reqs.txt", platforms=["linux-glibc"])   # a fresh object, as the TUI makes
    r = get_provider("pypi").resolve(src, context=ResolveContext(mirror_dir=tmp_path, max_connections=2,
                                                                 config_dir=conf))
    assert not r.errors, r.errors
