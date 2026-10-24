"""The site's blob store: every file it was sent, by checksum, hardlinked
into the served tree."""
import hashlib
import io

import pytest

from mmw.site.store import Store


def _cs(b):
    return hashlib.sha256(b).hexdigest()


def test_add_dedups_and_hashes(tmp_path):
    s = Store(tmp_path)
    cs, size = s.add_stream(io.BytesIO(b"hello"))
    assert (cs, size) == (_cs(b"hello"), 5) and s.has(cs)
    assert s.path(cs).read_bytes() == b"hello"
    assert s.add_stream(io.BytesIO(b"hello"))[0] == cs
    assert not list((tmp_path / ".mmw/store/.tmp").iterdir())


def test_expect_mismatch_keeps_nothing(tmp_path):
    s = Store(tmp_path)
    with pytest.raises(ValueError):
        s.add_stream(io.BytesIO(b"hello"), expect=_cs(b"other"))
    assert not s.has(_cs(b"hello")) and not list((tmp_path / ".mmw/store/.tmp").iterdir())


def test_place_hardlinks_and_replaces(tmp_path):
    s = Store(tmp_path)
    a, _ = s.add_stream(io.BytesIO(b"one"))
    b, _ = s.add_stream(io.BytesIO(b"two"))
    dest = tmp_path / "host/repo/f"
    s.place(a, dest)
    assert dest.stat().st_ino == s.path(a).stat().st_ino
    s.place(b, dest)
    assert dest.read_bytes() == b"two" and dest.stat().st_ino == s.path(b).stat().st_ino


def test_gc_keeps_only_kept(tmp_path):
    s = Store(tmp_path)
    a, _ = s.add_stream(io.BytesIO(b"keep"))
    b, _ = s.add_stream(io.BytesIO(b"drop!"))
    assert s.gc({a}) == (1, 5)
    assert s.has(a) and not s.has(b)
