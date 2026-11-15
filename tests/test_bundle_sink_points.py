"""The bundle sink for timeline points: rows are (path, checksum) with the
points holding them; each file goes once, by checksum, only if never sent."""
import hashlib
import json
import tarfile

from mmw.core.publisher.manifest import ManifestHeader, blob_member
from mmw.core.publisher.sinks import PointsSink


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _blob(tmp_path, name, data):
    p = tmp_path / "pool" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p, _sha(data), len(data)


def _header():
    return ManifestHeader(network="oak", created="20261115T000000Z", previous=None, full=False,
                          format=3, selection=True, points={})


def _read(out):
    with tarfile.open(out) as tar:
        names = tar.getnames()
        lines = tar.extractfile(".mmw/manifest.jsonl").read().decode().splitlines()
    return names, json.loads(lines[0]), [json.loads(x) for x in lines[1:]]


def test_each_file_once_by_checksum(tmp_path):
    src, cs, size = _blob(tmp_path, "a.deb", b"a" * 10)
    sink = PointsSink(tmp_path / "out.tar", _header(), sent=set())
    sink.link_point(src, "h/pool/a.deb", size, cs, "pkg", "R", 0)
    sink.link_point(src, "h/pool/a.deb", size, cs, "pkg", "R", 1)
    sink.link_point(src, "h/other/a.deb", size, cs, "pkg", "R", 1)
    sink.close()
    names, header, rows = _read(tmp_path / "out.tar")
    assert names == [".mmw/manifest.jsonl", blob_member(cs)] and header["file_count"] == 1
    assert {(r["p"], tuple(r["k"])) for r in rows} == {("h/pool/a.deb", (0, 1)), ("h/other/a.deb", (1,))}
    assert sink.sent_checksums == {cs} and sink.sent == 1


def test_a_file_sent_before_is_listed_not_carried(tmp_path):
    src, cs, size = _blob(tmp_path, "a.deb", b"a" * 10)
    sink = PointsSink(tmp_path / "out.tar", _header(), sent={cs})
    sink.link_point(src, "h/pool/a.deb", size, cs, "pkg", "R", 0)
    sink.close()
    names, header, rows = _read(tmp_path / "out.tar")
    assert names == [".mmw/manifest.jsonl"] and header["file_count"] == 0
    assert rows[0]["p"] == "h/pool/a.deb" and rows[0]["s"] == size
    assert sink.sent_checksums == set()


def test_generated_and_network_files_have_no_repository(tmp_path):
    src, cs, size = _blob(tmp_path, "k.asc", b"KEY")
    sink = PointsSink(tmp_path / "out.tar", _header(), sent=set(), signing_files={"tools": b"PRIV"})
    sink.link(src, "keys/tools/k.asc", size, cs, "meta")
    sink.write("nginx/mirror.conf", b"server {}")
    sink.close()
    names, _h, rows = _read(tmp_path / "out.tar")
    assert all("r" not in r and "k" not in r for r in rows)
    assert ".mmw/signing/tools.asc" in names and blob_member(_sha(b"server {}")) in names
    assert ".mmw/apply.py" not in names and ".mmw/delete.list" not in names


def test_a_damaged_pool_blob_writes_nothing(tmp_path):
    src, cs, size = _blob(tmp_path, "a.deb", b"a" * 10)
    sink = PointsSink(tmp_path / "out.tar", _header(), sent=set())
    sink.link_point(src, "h/pool/a.deb", size + 5, cs, "pkg", "R", 0)          # the pool says another size
    try:
        sink.close()
    except ValueError:
        pass
    assert not (tmp_path / "out.tar").exists() and sink.sent_checksums == set()
