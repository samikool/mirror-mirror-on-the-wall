"""A bundle comes as a tar file, a tar on stdin, or a stage directory — in
any member order, names with or without a leading ./ ."""
import io
import os
import tarfile

import pytest

from mmw.site.reader import SourceError, open_source


def _stage(tmp_path):
    d = tmp_path / "stage"
    (d / ".mmw").mkdir(parents=True)
    (d / ".mmw/manifest.jsonl").write_bytes(b"{}\n")
    (d / "h/r").mkdir(parents=True)
    (d / "h/r/a.deb").write_bytes(b"A")
    os.link(d / "h/r/a.deb", d / "h/r/b.deb")
    return d


def _read(members):
    return {m.name: (m.kind, m.fh.read() if m.fh else m.linkname) for m in members}


def test_tar_any_order_and_dot_slash(tmp_path):
    d = _stage(tmp_path)
    out = tmp_path / "b.tar"
    with tarfile.open(out, "w") as tar:
        for p in sorted((p for p in d.rglob("*") if p.is_file()), reverse=True):
            tar.add(p, arcname="./" + p.relative_to(d).as_posix())
    got = _read(open_source(str(out)))
    assert got[".mmw/manifest.jsonl"] == ("file", b"{}\n")
    assert got["h/r/a.deb"][0] in ("file", "link") and got["h/r/b.deb"][0] in ("file", "link")
    assert list(got) == ["h/r/b.deb", "h/r/a.deb", ".mmw/manifest.jsonl"]


def test_stdin_stream(tmp_path, monkeypatch):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        ti = tarfile.TarInfo("h/r/a.deb")
        ti.size = 1
        tar.addfile(ti, io.BytesIO(b"A"))
    buf.seek(0)

    class In:
        buffer = buf
    monkeypatch.setattr("sys.stdin", In)
    assert _read(open_source("-")) == {"h/r/a.deb": ("file", b"A")}


def test_stage_directory(tmp_path):
    got = _read(open_source(str(_stage(tmp_path))))
    assert got == {".mmw/manifest.jsonl": ("file", b"{}\n"), "h/r/a.deb": ("file", b"A"),
                   "h/r/b.deb": ("file", b"A")}


@pytest.mark.parametrize("bad", ["../escape", "/abs/path", "a/../../b"])
def test_unsafe_names_refused(tmp_path, bad):
    out = tmp_path / "b.tar"
    with tarfile.open(out, "w") as tar:
        ti = tarfile.TarInfo(bad)
        ti.size = 1
        tar.addfile(ti, io.BytesIO(b"x"))
    with pytest.raises(SourceError) as e:
        list(open_source(str(out)))
    assert e.value.code == 2


def test_symlinks_refused(tmp_path):
    out = tmp_path / "b.tar"
    with tarfile.open(out, "w") as tar:
        ti = tarfile.TarInfo("h/link")
        ti.type = tarfile.SYMTYPE
        ti.linkname = "/etc/passwd"
        tar.addfile(ti)
    with pytest.raises(SourceError) as e:
        list(open_source(str(out)))
    assert e.value.code == 2


def test_zstd_refused(tmp_path):
    out = tmp_path / "b.tar.zst"
    out.write_bytes(b"\x28\xb5\x2f\xfd" + b"\0" * 20)
    with pytest.raises(SourceError, match="decompress") as e:
        list(open_source(str(out)))
    assert e.value.code == 5
