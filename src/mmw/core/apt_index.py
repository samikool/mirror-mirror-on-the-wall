"""Build apt index files: a deb's control stanza, `Packages`, `Contents`, `Release`.

Pure and deterministic — the same inputs always give the same bytes, so a
repository rebuilt from an unchanged folder is the same repository version.
Signing is `mmw.core.signing`'s job; this only writes the text it signs.
"""
import gzip
import hashlib
import io
import lzma
import tarfile
from dataclasses import dataclass
from email.utils import formatdate
from pathlib import Path

import zstandard

from mmw.core import disk_io

# Fields the index writes itself: a deb's own control never carries them,
# and one that did would contradict the file actually served.
_INDEX_FIELDS = ("Filename", "Size", "MD5sum", "SHA256", "SHA1", "SHA512")


@dataclass(frozen=True)
class Entry:
    control: dict[str, str]
    filename: str            # the repo-relative path apt fetches
    size: int
    sha256: str
    md5: str


class _Bounded(io.RawIOBase):
    """One ar member of an open file, read without loading it."""

    def __init__(self, f, size: int):
        self._f, self._left = f, size

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        n = min(len(b), self._left)
        if n <= 0:
            return 0
        data = self._f.read(n)
        b[:len(data)] = data
        self._left -= len(data)
        return len(data)


def _member(f, path: Path, stem: str) -> tuple[str, int]:
    """Seek `f` to the start of the `{stem}.tar*` member, skipping the rest
    unread — a multi-GB deb must not be read whole to find its control."""
    if f.read(8) != b"!<arch>\n":
        raise ValueError(f"{path.name}: not a .deb (no ar archive header)")
    while True:
        header = f.read(60)
        if len(header) < 60:
            raise ValueError(f"{path.name}: not a .deb (no {stem}.tar member)")
        try:
            name = header[:16].decode().strip().rstrip("/")
            size = int(header[48:58].decode().strip())
        except ValueError:
            raise ValueError(f"{path.name}: not a .deb (bad ar member header)") from None
        if name.startswith(stem + ".tar"):
            return name, size
        f.seek(size + (size % 2), io.SEEK_CUR)


def _stream_tar(f, path: Path, stem: str) -> tarfile.TarFile:
    """The member as a streaming tar: decompressed as it is read, never held."""
    name, size = _member(f, path, stem)
    raw = io.BufferedReader(_Bounded(f, size), 1 << 20)
    if name.endswith(".zst"):
        return tarfile.open(fileobj=zstandard.ZstdDecompressor().stream_reader(raw), mode="r|")
    return tarfile.open(fileobj=raw, mode="r|*")


def _parse_stanza(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    key = None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line[0] in " \t" and key is not None:
            fields[key] += "\n" + line
        elif ":" in line:
            key, value = line.split(":", 1)
            fields[key] = value.strip()
    return fields


def read_control(path: Path) -> dict[str, str]:
    """The control stanza of a `.deb`, fields in file order."""
    text = None
    try:
        with disk_io.ops.open_rb(path) as f, _stream_tar(f, path, "control") as tar:
            for member in tar:
                if member.name.removeprefix("./") == "control" and member.isfile():
                    text = tar.extractfile(member).read().decode("utf-8", errors="replace")
                    break
    except (tarfile.TarError, lzma.LZMAError, zstandard.ZstdError, EOFError, OSError) as e:
        raise ValueError(f"{path.name}: unreadable control archive ({e})") from None
    if text is None:
        raise ValueError(f"{path.name}: not a .deb (control.tar has no control file)")
    fields = _parse_stanza(text)
    for required in ("Package", "Version", "Architecture"):
        if not fields.get(required):
            raise ValueError(f"{path.name}: control has no {required} field")
    return fields


def data_paths(path: Path) -> list[str]:
    """The files and symlinks a `.deb` installs, without the leading `./`."""
    try:
        with disk_io.ops.open_rb(path) as f, _stream_tar(f, path, "data") as tar:
            return sorted(m.name.removeprefix("./") for m in tar if m.isfile() or m.issym())
    except (tarfile.TarError, lzma.LZMAError, zstandard.ZstdError, EOFError, OSError) as e:
        raise ValueError(f"{path.name}: unreadable data archive ({e})") from None


def packages_text(entries: list[Entry]) -> bytes:
    def key(e: Entry):
        return (e.control["Package"], e.control["Version"], e.control["Architecture"])

    stanzas = []
    for e in sorted(entries, key=key):
        lines = [f"{k}: {v}" for k, v in e.control.items() if k not in _INDEX_FIELDS]
        lines += [f"Filename: {e.filename}", f"Size: {e.size}", f"MD5sum: {e.md5}",
                  f"SHA256: {e.sha256}"]
        stanzas.append("\n".join(lines) + "\n")
    return "\n".join(stanzas).encode()


def contents_text(rows: list[tuple[str, str]]) -> bytes:
    """`(path, "section/package")` rows in apt's Contents format."""
    owners: dict[str, list[str]] = {}
    for path, owner in rows:
        owners.setdefault(path, [])
        if owner not in owners[path]:
            owners[path].append(owner)
    return "".join(f"{p}  {','.join(sorted(o))}\n" for p, o in sorted(owners.items())).encode()


def compressed(name: str, data: bytes) -> dict[str, bytes]:
    gz = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=gz, mtime=0) as f:
        f.write(data)
    return {name: data, f"{name}.gz": gz.getvalue(), f"{name}.xz": lzma.compress(data)}


def release_text(fields: dict[str, str], files: dict[str, bytes]) -> bytes:
    lines = [f"{k}: {v}" for k, v in fields.items()]
    for label, algo in (("MD5Sum", hashlib.md5), ("SHA256", hashlib.sha256)):
        lines.append(f"{label}:")
        lines += [f" {algo(body).hexdigest()} {len(body):>16} {path}"
                  for path, body in sorted(files.items())]
    return ("\n".join(lines) + "\n").encode()


def rfc2822(ts: float) -> str:
    return formatdate(ts, usegmt=True).replace("GMT", "UTC")
