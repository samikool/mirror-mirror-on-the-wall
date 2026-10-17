"""Warming the pool database into the page cache before heavy jobs."""
import os
import resource
import threading

import pytest

from mmw.core import db_warm
from mmw.core.db_warm import warm_database
from test_bundle import seeded  # noqa: F401 — the shared seeded-pool fixture


@pytest.fixture
def disk_dir():
    """Files on a real filesystem: tmpfs pages (often /tmp) cannot be evicted,
    so a file there could never start cold."""
    import shutil
    import tempfile
    from pathlib import Path
    base = Path.home() / ".cache"
    base.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix="mmw-warm-", dir=base))
    yield d
    shutil.rmtree(d, ignore_errors=True)


def _file(tmp_path, mib: int):
    p = tmp_path / "mmw.db"
    with open(p, "wb") as f:
        for _ in range(mib):
            f.write(os.urandom(1 << 20))
        f.flush()
        os.fsync(f.fileno())
    fd = os.open(p, os.O_RDONLY)
    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    os.close(fd)
    if db_warm.resident_fraction(p) > 0.5:
        pytest.skip("this filesystem keeps the file cached; cannot start cold")
    return p


def test_warming_reads_the_file_into_the_cache(disk_dir):
    p = _file(disk_dir, 16)
    before = db_warm.resident_fraction(p)
    assert warm_database(p).status == "warmed"
    # Not ">= 0.95": under memory pressure the kernel reclaims once-read pages
    # quickly (a busy dev box did, mid-suite); the job's own reads keep them.
    assert db_warm.resident_fraction(p) >= before + 0.25


def test_a_resident_database_is_not_read_again(disk_dir, monkeypatch):
    p = _file(disk_dir, 4)
    monkeypatch.setattr(db_warm, "resident_fraction", lambda _p: 0.99)
    monkeypatch.setattr(db_warm, "_CHUNK", None)       # any read would raise
    result = warm_database(p)
    assert result.status == "resident" and result.resident_before == 0.99


def test_a_database_too_big_for_available_memory_is_skipped(disk_dir):
    p = _file(disk_dir, 4)
    result = warm_database(p, mem_fraction=1e-9)
    assert result.status == "too-big"
    assert "skipped" in result.message


def test_stop_ends_warming_before_it_reads(disk_dir):
    p = _file(disk_dir, 4)
    stop = threading.Event()
    stop.set()
    assert warm_database(p, stop_event=stop).status == "stopped"


def test_unreadable_meminfo_skips_warming_without_failing(disk_dir, monkeypatch):
    p = _file(disk_dir, 4)
    monkeypatch.setattr(db_warm, "_MEMINFO", disk_dir / "no-such-meminfo")
    assert warm_database(p).status == "unavailable"


def test_a_missing_database_is_unavailable_not_an_error(tmp_path):
    assert warm_database(tmp_path / "absent.db").status == "unavailable"


def test_warming_does_not_grow_process_memory_with_the_file(disk_dir):
    p = _file(disk_dir, 64)
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    assert warm_database(p).status == "warmed"
    grown_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
    assert grown_kib < 16 * 1024


@pytest.mark.parametrize("job", ["publish", "bundle"])
def test_publish_and_bundle_warm_the_configured_database_once(job, monkeypatch, seeded, tmp_path):  # noqa: F811
    from mmw.core.config import NetworkConfig
    from mmw.core.pool import database_path
    from mmw.core.publisher import publish_network
    from mmw.core.publisher.bundle import bundle_network

    mirror_dir, cfg = seeded
    calls = []
    monkeypatch.setattr(db_warm, "warm_database",
                        lambda path, **kw: calls.append(path) or db_warm.WarmResult(
                            "resident", 1.0, 0.0, "already in memory"))
    net = NetworkConfig(name="lab", root=tmp_path / "pub", bundle_dir=tmp_path / "b")
    if job == "publish":
        publish_network(net, cfg, mirror_dir)
    else:
        bundle_network(net, cfg, mirror_dir, full=True)
    assert calls == [database_path(mirror_dir)]



def test_a_first_sync_with_no_database_yet_says_nothing(tmp_path):
    said = []
    result = db_warm.warm_for_job(tmp_path / "new-pool", say=said.append)
    assert result.status == "unavailable"
    assert said == []


def test_a_database_using_most_of_available_memory_is_still_warmed(disk_dir, monkeypatch):
    """Page cache is reclaimable — warming cannot exhaust memory — so the only
    reason to skip is a file that would not fit. A 3.1 GB database with 6 GB
    available used to be skipped by a 50 % rule."""
    p = _file(disk_dir, 4)
    size = p.stat().st_size
    monkeypatch.setattr(db_warm, "_available_bytes", lambda: int(size / 0.95))  # file = 95 %
    assert warm_database(p).status == "too-big"
    monkeypatch.setattr(db_warm, "_available_bytes", lambda: int(size / 0.8))   # file = 80 %
    assert warm_database(p).status == "warmed"
