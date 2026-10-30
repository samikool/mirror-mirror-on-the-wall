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


def _ar_members(path: Path) -> dict[str, bytes]:
    data = disk_io.ops.read_bytes(path)
    if not data.startswith(b"!<arch>\n"):
        raise ValueError(f"{path.name}: not a .deb (no ar archive header)")
    members, pos = {}, 8
    while pos + 60 <= len(data):
        header = data[pos:pos + 60]
        try:
            name = header[:16].decode().strip().rstrip("/")
            size = int(header[48:58].decode().strip())
        except ValueError:
            raise ValueError(f"{path.name}: not a .deb (bad ar member header)") from None
        pos += 60
        members[name] = data[pos:pos + size]
        pos += size + (size % 2)
    return members


def _open_tar(path: Path, members: dict[str, bytes], stem: str) -> tarfile.TarFile:
    for name, body in members.items():
        if not name.startswith(stem + ".tar"):
            continue
        if name.endswith(".zst"):
            body = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(body)).read()
            return tarfile.open(fileobj=io.BytesIO(body), mode="r:")
        return tarfile.open(fileobj=io.BytesIO(body), mode="r:*")
    raise ValueError(f"{path.name}: not a .deb (no {stem}.tar member)")


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
    members = _ar_members(path)
    try:
        with _open_tar(path, members, "control") as tar:
            member = next((m for m in tar.getmembers() if m.name.removeprefix("./") == "control"), None)
            if member is None:
                raise ValueError(f"{path.name}: not a .deb (control.tar has no control file)")
            text = tar.extractfile(member).read().decode("utf-8", errors="replace")
    except (tarfile.TarError, lzma.LZMAError, zstandard.ZstdError, EOFError, OSError) as e:
        raise ValueError(f"{path.name}: unreadable control archive ({e})") from None
    fields = _parse_stanza(text)
    for required in ("Package", "Version", "Architecture"):
        if not fields.get(required):
            raise ValueError(f"{path.name}: control has no {required} field")
    return fields


def data_paths(path: Path) -> list[str]:
    """The files and symlinks a `.deb` installs, without the leading `./`."""
    members = _ar_members(path)
    try:
        with _open_tar(path, members, "data") as tar:
            return sorted(m.name.removeprefix("./") for m in tar.getmembers() if m.isfile() or m.issym())
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
