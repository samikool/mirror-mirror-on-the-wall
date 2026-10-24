"""The bundle's writing half: tar or directory, same members either way —
and the apply-network bundle unchanged by the split."""
import json
import os
from pathlib import Path

from mmw.core import config
from mmw.core.publisher.bundle import bundle_network

from bundle_golden import describe
from test_bundle import seeded  # noqa: F401 — the shared pool fixture

GOLDEN = Path(__file__).parent / "data" / "bundle_members_golden.json"


def test_bundle_bytes_unchanged_by_refactor(seeded, tmp_path):  # noqa: F811
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out", certs=False)
    result = bundle_network(net, cfg, mirror_dir, from_ref=None)
    got = describe(result.out_path)
    if os.environ.get("MMW_WRITE_GOLDEN"):
        GOLDEN.write_text(json.dumps(got, indent=1) + "\n")
    assert got == json.loads(GOLDEN.read_text())


def _write_three(w, blob_src: Path):
    w.add_bytes(".mmw/manifest.jsonl", b"{}\n")
    w.add_bytes("tls/x.key", b"secret", mode=0o600)
    with open(blob_src, "rb") as fh:
        w.add_blob("h/p/a.deb", fh, blob_src.stat().st_size, src=blob_src)
    w.add_hardlink("h/p/by-hash/aa", "h/p/a.deb")


def test_tar_members_round_trip(tmp_path):
    import tarfile
    from mmw.core.publisher.members import TarMembers
    blob = tmp_path / "blob"
    blob.write_bytes(b"payload")
    final, tmp = tmp_path / "x.tar", tmp_path / "x.tar.tmp"
    w = TarMembers(open(tmp, "wb"), final_path=final, tmp_path=tmp)
    _write_three(w, blob)
    assert not final.exists()
    w.commit()
    assert final.exists() and not tmp.exists()
    with tarfile.open(final) as tar:
        names = [(m.name, m.mode, m.linkname or None) for m in tar]
        assert names == [(".mmw/manifest.jsonl", 0o644, None), ("tls/x.key", 0o600, None),
                         ("h/p/a.deb", 0o644, None), ("h/p/by-hash/aa", 0o644, "h/p/a.deb")]
        assert tar.extractfile("h/p/a.deb").read() == b"payload"


def test_tar_members_abort_leaves_nothing(tmp_path):
    from mmw.core.publisher.members import TarMembers
    final, tmp = tmp_path / "x.tar", tmp_path / "x.tar.tmp"
    w = TarMembers(open(tmp, "wb"), final_path=final, tmp_path=tmp)
    w.add_bytes("a", b"1")
    w.abort()
    assert not final.exists() and not tmp.exists()


def test_dir_members_commit_and_abort(tmp_path):
    from mmw.core.publisher.members import DirMembers
    w = DirMembers(tmp_path / "stage")
    w.add_bytes(".mmw/manifest.jsonl", b"{}\n")
    w.abort()
    assert not (tmp_path / "stage").exists() and not (tmp_path / "stage.tmp").exists()


def test_dir_members_hardlinks_pool_blob(tmp_path):
    from mmw.core.publisher.members import DirMembers
    blob = tmp_path / "blob"
    blob.write_bytes(b"payload")
    w = DirMembers(tmp_path / "stage")
    _write_three(w, blob)
    w.commit()
    stage = tmp_path / "stage"
    assert not (tmp_path / "stage.tmp").exists()
    assert (stage / "h/p/a.deb").stat().st_ino == blob.stat().st_ino
    assert (stage / "h/p/by-hash/aa").stat().st_ino == blob.stat().st_ino
    assert (stage / "tls/x.key").stat().st_mode & 0o777 == 0o600
    assert (stage / ".mmw/manifest.jsonl").read_bytes() == b"{}\n"
    assert w.copied_bytes == 0


def test_dir_members_copies_when_it_cannot_link(tmp_path, monkeypatch):
    from mmw.core import disk_io
    from mmw.core.publisher.members import DirMembers
    monkeypatch.setattr(disk_io.ops, "link_into", lambda src, dst: False)
    blob = tmp_path / "blob"
    blob.write_bytes(b"payload")
    w = DirMembers(tmp_path / "stage")
    _write_three(w, blob)
    w.commit()
    assert (tmp_path / "stage/h/p/a.deb").read_bytes() == b"payload"
    assert (tmp_path / "stage/h/p/a.deb").stat().st_ino != blob.stat().st_ino
    assert w.copied_bytes == 2 * len(b"payload")              # the blob, and its hardlink entry
