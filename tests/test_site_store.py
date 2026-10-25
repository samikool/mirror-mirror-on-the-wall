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


def test_content_already_stored_is_checked_not_written(tmp_path, monkeypatch):
    """A blob the store has is hashed to check it, never written again (a
    re-applied 120 GB bundle wrote every byte to a temp file and threw it away)."""
    import io

    from mmw.core import disk_io
    s = Store(tmp_path)
    data = b"x" * 1000
    checksum, _ = s.add_stream(io.BytesIO(data))
    real = disk_io.ops.open_file
    writes = []
    monkeypatch.setattr(disk_io.ops, "open_file",
                        lambda p, mode="r", *a, **k: (writes.append(p) if "w" in mode else None) or real(p, mode, *a, **k))
    assert s.add_stream(io.BytesIO(data), expect=checksum) == (checksum, 1000)
    assert writes == []
    with pytest.raises(ValueError):
        s.add_stream(io.BytesIO(b"y" * 1000), expect=checksum)        # damaged bytes still refused


def test_reapplying_writes_nothing(tmp_path, monkeypatch):
    from mmw.core import disk_io
    from mmw.site.apply import apply_bundle
    from test_bundle_mmw import _mmw
    from test_dry_run_totals import two_mirrors as _tm
    from test_site_apply import _bundle
    from mmw.site import home as H
    cfg, net, md = _mmw(_tm.__wrapped__(tmp_path))
    home = H.init_root(tmp_path / "srv", name="main", set_id="s1", network=net.name)
    b = str(_bundle(cfg, net, md, tmp_path / "1", from_ref=None))
    apply_bundle(home, b, say=lambda *a: None)
    real = disk_io.ops.open_file
    writes = []
    monkeypatch.setattr(disk_io.ops, "open_file",
                        lambda p, mode="r", *a, **k: (writes.append(p) if "w" in mode and ".tmp" in str(p) else None)
                        or real(p, mode, *a, **k))
    apply_bundle(home, b, say=lambda *a: None)
    assert [p for p in writes if "/.mmw/store/" in str(p)] == []


def test_a_checksum_is_never_a_path(tmp_path):
    s = Store(tmp_path)
    for bad in ("/home/sam/.ssh/id_ed25519", "../../etc/passwd", "ab" * 31 + "/x", "zz" * 32):
        with pytest.raises(ValueError):
            s.path(bad)
