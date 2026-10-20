"""The stream route: files opened ahead on the read lane, read in order on the
caller's thread; every handle closed, consumed or not."""
import hashlib
import threading

import pytest

from mmw.core import disk_io
from mmw.core.disk_io import DiskJob, Limits


def _files(tmp_path, n):
    paths = []
    for i in range(n):
        p = tmp_path / f"f{i}"
        p.write_bytes(f"body {i}".encode() * (i + 1))
        paths.append(p)
    return paths


def test_stream_open_in_order_with_readahead(tmp_path):
    paths = _files(tmp_path, 20)
    with DiskJob("t", limits=Limits(readahead=4)) as job:
        got = []
        for path, fh in job.stream_open(paths):
            with fh:
                got.append((path, fh.read()))
    assert got == [(p, p.read_bytes()) for p in paths]


def test_stream_open_opens_ahead(tmp_path, monkeypatch):
    paths = _files(tmp_path, 8)
    real, opened, third = disk_io.ops.open_rb, [], threading.Event()

    def open_rb(path):
        opened.append(path)
        if path == paths[3]:
            third.set()
        return real(path)

    monkeypatch.setattr(disk_io.ops, "open_rb", open_rb)
    with DiskJob("t", limits=Limits(readahead=4)) as job:
        gen = job.stream_open(paths)
        _path, first = next(gen)
        assert third.wait(5), "file 3 was opened while the consumer still holds file 0"
        first.close()
        for _p, fh in gen:
            fh.close()


def test_stream_open_closes_unconsumed_handles_on_abort(tmp_path, monkeypatch):
    paths = _files(tmp_path, 30)
    real, handles, lock = disk_io.ops.open_rb, [], threading.Lock()

    def open_rb(path):
        fh = real(path)
        with lock:
            handles.append(fh)
        return fh

    monkeypatch.setattr(disk_io.ops, "open_rb", open_rb)
    with DiskJob("t", limits=Limits(readahead=16)) as job:
        gen = job.stream_open(paths)
        _path, first = next(gen)
        with pytest.raises(RuntimeError):
            with first:
                raise RuntimeError("the vanished-blob abort")
        gen.close()
    assert len(handles) > 1
    assert all(fh.closed for fh in handles)


def test_stream_open_readahead_zero_is_inline(tmp_path, monkeypatch):
    paths = _files(tmp_path, 5)
    real, threads = disk_io.ops.open_rb, []
    monkeypatch.setattr(disk_io.ops, "open_rb",
                        lambda p: (threads.append(threading.current_thread()), real(p))[1])
    with DiskJob("t", limits=Limits(readahead=0)) as job:
        for _p, fh in job.stream_open(paths):
            fh.close()
    assert threads == [threading.main_thread()] * 5


def test_hash_files_matches_hashlib(tmp_path):
    paths = _files(tmp_path, 6)
    with DiskJob("t", limits=Limits(readahead=3)) as job:
        got = dict(job.hash_files(paths))
        assert job.stats.routes["stream"].bytes == sum(p.stat().st_size for p in paths)
    assert got == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    assert disk_io.hash_file(paths[2]) == got[paths[2]]


def test_hash_files_missing_yields_error(tmp_path):
    paths = _files(tmp_path, 2)
    with DiskJob("t", limits=Limits(readahead=2)) as job:
        got = dict(job.hash_files([paths[0], tmp_path / "missing", paths[1]]))
    assert isinstance(got[tmp_path / "missing"], OSError)
    assert got[paths[1]] == hashlib.sha256(paths[1].read_bytes()).hexdigest()


def test_write_if_changed_writes_only_new_bytes(tmp_path):
    dst = tmp_path / "a" / "b" / "page.html"
    assert disk_io.ops.write_if_changed(dst, b"one") is True
    mtime = dst.stat().st_mtime_ns
    assert disk_io.ops.write_if_changed(dst, b"one") is False
    assert dst.stat().st_mtime_ns == mtime            # unchanged page keeps its mtime
    assert disk_io.ops.write_if_changed(dst, b"two") is True
    assert dst.read_bytes() == b"two"
    assert sorted(p.name for p in dst.parent.iterdir()) == ["page.html"]   # no staging left
