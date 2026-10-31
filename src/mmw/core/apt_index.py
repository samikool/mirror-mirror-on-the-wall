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
            return sorted(m.name.removeprefix("./") for m in tar if m.isfile() or m.issym() or m.islnk())
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


# ── reading and rewriting upstream indexes (per-network layers) ───────────────

def parse_packages(text: str) -> list[dict[str, str]]:
    """A `Packages` file's stanzas, in order, fields in order."""
    return [_parse_stanza(block) for block in text.split("\n\n") if block.strip()]


def release_body(data: bytes) -> bytes:
    """The Release text from `Release` or a clearsigned `InRelease`."""
    if not data.startswith(b"-----BEGIN PGP SIGNED MESSAGE-----"):
        return data
    head, _, rest = data.partition(b"\n\n")
    body = rest.split(b"\n-----BEGIN PGP SIGNATURE-----", 1)[0]
    lines = [ln[2:] if ln.startswith(b"- ") else ln for ln in body.split(b"\n")]
    return b"\n".join(lines) + b"\n"


_HASHES = {"MD5Sum": hashlib.md5, "SHA1": hashlib.sha1, "SHA256": hashlib.sha256,
           "SHA512": hashlib.sha512}


def rewrite_release(release: bytes, changed: dict[str, bytes], *,
                    drop_valid_until: bool = False) -> bytes:
    """`release` with every hash section's entry for each `changed` path
    replaced (new paths appended, sorted); every other line exactly as
    upstream wrote it. Date and Valid-Until are kept unless asked."""
    out: list[str] = []
    section = None
    pending: set[str] = set()

    def flush():
        if section in _HASHES:
            algo = _HASHES[section]
            for path in sorted(pending):
                body = changed[path]
                out.append(f" {algo(body).hexdigest()} {len(body):>16} {path}")

    for line in release.decode("utf-8", errors="replace").splitlines():
        if line.startswith(" ") and section is not None:
            parts = line.split()
            if section in _HASHES and len(parts) == 3 and parts[2] in changed:
                pending.discard(parts[2])
                body = changed[parts[2]]
                out.append(f" {_HASHES[section](body).hexdigest()} {len(body):>16} {parts[2]}")
            else:
                out.append(line)
            continue
        flush()
        key = line.split(":", 1)[0]
        section = key if line.endswith(":") else None
        pending = set(changed)
        if drop_valid_until and key == "Valid-Until":
            continue
        out.append(line)
    flush()
    return ("\n".join(out) + "\n").encode()


def _order(c: str) -> int:
    if c == "~":
        return -1
    if c.isdigit():
        return 0
    if c.isalpha():
        return ord(c)
    return ord(c) + 256


def _cmp_part(a: str, b: str) -> int:
    """dpkg's verrevcmp: alternating non-digit and digit runs."""
    i = j = 0
    while i < len(a) or j < len(b):
        first_diff = 0
        while (i < len(a) and not a[i].isdigit()) or (j < len(b) and not b[j].isdigit()):
            ac = _order(a[i]) if i < len(a) else 0
            bc = _order(b[j]) if j < len(b) else 0
            if ac != bc:
                return ac - bc
            i += 1
            j += 1
        while i < len(a) and a[i] == "0":
            i += 1
        while j < len(b) and b[j] == "0":
            j += 1
        while i < len(a) and a[i].isdigit() and j < len(b) and b[j].isdigit():
            if not first_diff:
                first_diff = ord(a[i]) - ord(b[j])
            i += 1
            j += 1
        if i < len(a) and a[i].isdigit():
            return 1
        if j < len(b) and b[j].isdigit():
            return -1
        if first_diff:
            return first_diff
    return 0


def _split_version(v: str) -> tuple[int, str, str]:
    epoch, _, rest = v.rpartition(":") if ":" in v else ("0", "", v)
    upstream, _, revision = rest.rpartition("-") if "-" in rest else (rest, "", "")
    return int(epoch or 0), upstream, revision


def version_cmp(a: str, b: str) -> int:
    """dpkg's version ordering: < 0, 0 or > 0."""
    ea, ua, ra = _split_version(a)
    eb, ub, rb = _split_version(b)
    if ea != eb:
        return ea - eb
    return _cmp_part(ua, ub) or _cmp_part(ra, rb)
