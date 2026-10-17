"""read_all: many small pool files read with requests in flight together —
a spinning disk reorders a queue of reads, it cannot reorder one at a time."""
import threading

from mmw.core.bulk_read import read_all


def test_it_returns_each_file_s_bytes_or_its_error(tmp_path):
    (tmp_path / "a").write_bytes(b"A")
    (tmp_path / "b").write_bytes(b"B")
    got = read_all([tmp_path / "a", tmp_path / "missing", tmp_path / "b"])
    assert got[tmp_path / "a"] == b"A" and got[tmp_path / "b"] == b"B"
    assert isinstance(got[tmp_path / "missing"], OSError)


def test_reads_are_in_flight_together():
    # Four reads that each wait for the other three: one at a time, the
    # barrier breaks instead.
    barrier = threading.Barrier(4, timeout=5)

    def reader(path):
        barrier.wait()
        return str(path).encode()
    got = read_all(["w", "x", "y", "z"], workers=4, reader=reader)
    assert got == {"w": b"w", "x": b"x", "y": b"y", "z": b"z"}
