"""The requirements list through the real sync engine, offline: real uv
resolves against recorded PyPI served on 127.0.0.1, then sync and verify."""
import asyncio
import concurrent.futures
import json

import pytest

from mmw.core import config, downloader
from mmw.core.pool import Pool
from mmw.core.resolve import resolve_sources
from mmw.core.sync_engine import sync_mirror
from mmw.core.verify import verify_mirror
from mmw.providers.base import ResolveContext
from mmw.providers.pypi import index
from mmw.providers.pypi import uv as uvmod
from pypi_fakes import file_entry, from_fixtures, metadata_text, real_sdist
from pypi_server import serve


@pytest.fixture
def up(monkeypatch):
    fake = from_fixtures()
    fake.add("srconly", [real_sdist("srconly", "1.0", build_requires=["flit-core>=3"])])
    fake.add("flit-core", [file_entry("flit_core-3.9.0-py3-none-any.whl",
                                      metadata=metadata_text("flit-core", "3.9.0"))])
    monkeypatch.setattr(index, "_client", fake.client)
    monkeypatch.setattr(downloader, "_thread_client", fake.client)
    with serve(fake) as base:
        monkeypatch.setattr(uvmod, "INDEX_URL", f"{base}/simple")
        yield fake


def _src(reqs):
    return config.PypiSource(provider="pypi", pythons=["3.12"], platforms=["linux-glibc", "windows"],
                             arches=["amd64"], requirements=reqs)


def _resolve_and_sync(tmp_path, src):
    units, notes, errors = resolve_sources([src], context=ResolveContext(mirror_dir=tmp_path, max_connections=4))
    assert not errors, errors
    ex = concurrent.futures.ThreadPoolExecutor(4)

    async def go():
        with Pool(tmp_path) as pool:
            return [await sync_mirror(u, pool=pool, mirror_dir=tmp_path, max_connections=4, executor=ex)
                    for u in units]
    try:
        return units, asyncio.run(go())
    finally:
        ex.shutdown()


def _refs(tmp_path, unit):
    with Pool(tmp_path, read_only=True) as pool:
        return sorted(p.rsplit("/", 1)[1] for p in pool.ref_map(unit.name) if not p.startswith(".pypi-meta/"))


def test_requirements_sync_then_verify(up, tmp_path):
    units, summaries = _resolve_and_sync(tmp_path, _src(["requests[socks]", "cryptography"]))
    assert [u.name for u in units] == ["pypi-req-universal", "pypi-req-3.12-linux-glibc-amd64",
                                       "pypi-req-3.12-windows-amd64"]
    assert all(s.ok for s in summaries), [s.errors + s.metadata_errors for s in summaries]
    with Pool(tmp_path, read_only=True) as pool:
        for u in units:
            assert verify_mirror(u, tmp_path, pool).complete, u.name
    universal = _refs(tmp_path, units[0])
    assert any(n.lower().startswith("pysocks-") for n in universal)          # the extra, honoured by uv
    assert any(n.startswith("requests-") for n in universal)
    assert any(n.startswith("cryptography-") and "win_amd64" in n for n in _refs(tmp_path, units[2]))


def test_sdist_only_requirement_mirrors_its_build_tools(up, tmp_path):
    units, summaries = _resolve_and_sync(tmp_path, _src(["srconly"]))
    assert all(s.ok for s in summaries)
    universal = _refs(tmp_path, units[0])
    assert "srconly-1.0.tar.gz" in universal and "flit_core-3.9.0-py3-none-any.whl" in universal
    with Pool(tmp_path, read_only=True) as pool:
        report = json.loads(pool.resolve(units[0].name, ".pypi-meta/report.json").read_bytes())
    assert [r[1] for r in report["built_from_source"]] == ["srconly==1.0", "srconly==1.0"]


def test_dry_run_lists_req_units(up, tmp_path):
    from typer.testing import CliRunner

    from mmw.cli import app
    config.save(config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"), sources=[_src(["six"])]),
                tmp_path)
    r = CliRunner().invoke(app, ["--config", str(tmp_path), "dry-run"])
    assert r.exit_code == 0, r.output
    assert "pypi-req-universal" in r.output and "pypi-req-3.12-windows-amd64" in r.output
