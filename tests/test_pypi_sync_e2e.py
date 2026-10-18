"""A PyPI source through the real sync engine, offline, on recorded PyPI data:
resolve → dry-run → sync → verify, then a re-sync after an upstream release."""
import asyncio
import concurrent.futures
import json

import pytest

from mmw.core import config, downloader
from mmw.core.pool import Pool
from mmw.core.resolve import resolve_sources
from mmw.core.sync_engine import classify_mirror, sync_mirror
from mmw.core.verify import verify_mirror
from mmw.providers import get_provider
from mmw.providers.base import ResolveContext
from mmw.providers.pypi import index
from pypi_fakes import file_entry, from_fixtures, metadata_text


@pytest.fixture
def up(monkeypatch):
    fake = from_fixtures()
    monkeypatch.setattr(index, "_client", fake.client)
    monkeypatch.setattr(downloader, "_thread_client", fake.client)
    return fake


def _src():
    return config.PypiSource(provider="pypi", pythons=["3.10", "3.12"],
                             platforms=["linux-glibc", "linux-musl", "windows"], arches=["amd64"], all=True)


def _resolve(tmp_path):
    units, notes, errors = resolve_sources([_src()], context=ResolveContext(mirror_dir=tmp_path, max_connections=4))
    assert not errors, errors
    return units


def _sync(tmp_path, units):
    ex = concurrent.futures.ThreadPoolExecutor(4)

    async def go():
        with Pool(tmp_path) as pool:
            return [await sync_mirror(u, pool=pool, mirror_dir=tmp_path, max_connections=4, executor=ex)
                    for u in units]
    try:
        return asyncio.run(go())
    finally:
        ex.shutdown()


def _unit(units, key):
    return next(u for u in units if u.name == f"pypi-all-{key}")


def _filenames(units, key):
    cat = get_provider("pypi").catalog["https://pypi.org/simple"]
    return sorted(cat.keep.files.get(key, {}))


def _report(units, tmp_path, key):
    with Pool(tmp_path, read_only=True) as pool:
        return json.loads(pool.resolve(_unit(units, key).name, ".pypi-meta/report.json").read_bytes())


def test_dry_run_sizes_are_exact(up, tmp_path):
    units = _resolve(tmp_path)
    cat = get_provider("pypi").catalog["https://pypi.org/simple"]
    ex = concurrent.futures.ThreadPoolExecutor(2)

    async def go():
        with Pool(tmp_path, read_only=True) as pool:
            return {u.name: await classify_mirror(u, pool, ex) for u in units}
    try:
        stats = asyncio.run(go())
    finally:
        ex.shutdown()
    for u in units:
        key = "universal" if not u.dims else u.name.removeprefix("pypi-all-")
        assert stats[u.name].sizes_known
        assert stats[u.name].total_size == sum(f.size for f in cat.keep.files.get(key, {}).values())


def test_sync_then_verify(up, tmp_path):
    units = _resolve(tmp_path)
    summaries = _sync(tmp_path, units)
    assert all(s.ok for s in summaries), [s.errors + s.metadata_errors for s in summaries]
    with Pool(tmp_path, read_only=True) as pool:
        for u in units:
            assert verify_mirror(u, tmp_path, pool).complete, u.name
        linux = _unit(units, "3.12-linux-glibc-amd64")
        refs = pool.ref_map(linux.name)
        wheels = [p for p in refs if p.endswith(".whl")]
        assert wheels and all(p + ".metadata" in refs for p in wheels)


def test_closure_facts_on_real_data(up, tmp_path):
    units = _resolve(tmp_path)
    _sync(tmp_path, units)

    def numpy(key):
        return {f.split("-")[1] for f in _filenames(units, key) if f.startswith("numpy-")}
    # Requires-Python cutoff: 3.10 keeps an older numpy than 3.12 (latest needs >=3.12).
    (n310,), (n312,) = numpy("3.10-linux-glibc-amd64"), numpy("3.12-linux-glibc-amd64")
    from packaging.version import Version
    assert Version(n310) < Version(n312)
    # Windows-only projects stay out of the Linux units.
    assert any(f.startswith("pywin32-") for f in _filenames(units, "3.12-windows-amd64"))
    assert not any(f.startswith(("pywin32-", "psycopg2-")) for f in _filenames(units, "3.12-linux-glibc-amd64"))
    # cryptography needs typing-extensions on 3.10 only; it is not recorded, so it is reported.
    unsat310 = _report(units, tmp_path, "3.10-linux-glibc-amd64")["unsatisfiable"]
    assert any(r[1].startswith("cryptography==") and r[2].startswith("typing-extensions") for r in unsat310)
    unsat312 = _report(units, tmp_path, "3.12-linux-glibc-amd64")["unsatisfiable"]
    assert not any(r[2].startswith("typing-extensions") for r in unsat312)
    # requests' use-chardet-on-py3 extra names an unrecorded project: an extras gap, not a failure.
    gaps = _report(units, tmp_path, "3.12-linux-glibc-amd64")["extras_gaps"]
    assert any(r[1].startswith("requests==") and r[2] == "use-chardet-on-py3" for r in gaps)


def test_resync_after_upstream_bump_retains_old(up, tmp_path):
    # PySocks: nothing pins it, so a new release makes the old one leave the keep set
    # (urllib3 would not — requests and botocore keep its <3 line).
    units = _resolve(tmp_path)
    _sync(tmp_path, units)
    uni = _unit(units, "universal")
    with Pool(tmp_path, read_only=True) as pool:
        before = {p for p in pool.ref_map(uni.name) if "/pysocks-" in p.lower()}
    up.projects["pysocks"]["files"].append(
        file_entry("pysocks-9.0.0-py3-none-any.whl", metadata=metadata_text("pysocks", "9.0.0")))
    up.projects["pysocks"]["serial"] += 1
    up.requests.clear()
    units = _resolve(tmp_path)
    assert up.project_requests() == ["pysocks"]
    assert all(s.ok for s in _sync(tmp_path, units))
    with Pool(tmp_path, read_only=True) as pool:
        after = {p for p in pool.ref_map(uni.name) if "/pysocks-" in p.lower()}
        history = pool.resolve(uni.name, ".pypi-meta/history.json.gz").read_bytes()
    assert before <= after and any(p.endswith("pysocks-9.0.0-py3-none-any.whl") for p in after)
    from mmw.providers.pypi.history import load
    left = [e.filename for e in load(history).values() if e.left_at]
    assert left and all(f.lower().startswith("pysocks-") for f in left)


def test_dry_run_command_lists_pypi_units(up, tmp_path):
    from typer.testing import CliRunner

    from mmw.cli import app
    config.save(config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"), sources=[_src()]), tmp_path)
    r = CliRunner().invoke(app, ["--config", str(tmp_path), "dry-run"])
    assert r.exit_code == 0, r.output
    assert "pypi-all-3.12-linux-glibc-amd64" in r.output and "pypi-all-universal" in r.output
