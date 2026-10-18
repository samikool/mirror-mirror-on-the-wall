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
    assert [label for label, _ in d.fields] == ["Provider", "Pythons", "Platforms", "Arches", "OS floors"]
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
