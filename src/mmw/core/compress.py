import gzip
import io
import lzma

import zstandard

# Upper bound on what a single decompressed metadata file may occupy.
#
# Every caller feeds this bytes that came straight off an upstream repository
# — `Packages.xz`, `Contents-*.gz`, a pooled index being re-parsed by verify —
# and the old one-shot `lzma.decompress()` / `gzip.decompress()` calls had no
# ceiling at all. A ~1 MB crafted `Packages.xz` expanding to tens of gigabytes
# would take the sync process with it (and in the TUI, the whole app), which
# is a denial of service any mirrored repository could inflict.
#
# 4 GiB is far above anything real: Ubuntu's largest uncompressed index,
# `Contents-amd64`, is comfortably under 1 GiB, and a genuine index that
# exceeded this would be a repository mmw could not usefully mirror anyway.
MAX_DECOMPRESSED = 4 * 1024 * 1024 * 1024

_CHUNK = 1 << 20


class DecompressionTooLarge(ValueError):
    """Decompressed output exceeded MAX_DECOMPRESSED — refused, not buffered."""


def _read_bounded(reader, limit: int, name: str) -> bytes:
    """Read `reader` to EOF, refusing once `limit` bytes have been produced.

    Reads one chunk past the limit on purpose: that is what distinguishes
    "exactly at the ceiling" from "over it" without trusting a declared size
    (zstd frames from Fedora omit the content-size header entirely, which is
    why the zstd path already had to stream).
    """
    out = bytearray()
    while True:
        chunk = reader.read(_CHUNK)
        if not chunk:
            return bytes(out)
        out += chunk
        if len(out) > limit:
            raise DecompressionTooLarge(
                f"{name}: decompressed output exceeded {limit} bytes — refusing "
                f"to buffer it (a compressed index this expansive is not a real one)"
            )


def decompress(data: bytes, name: str, limit: int = MAX_DECOMPRESSED) -> bytes:
    """Decompress bytes based on file extension in name (URL or filename).

    Handles .zst / .xz / .gz; returns data unchanged for any other suffix.
    Raises `DecompressionTooLarge` past `limit` — see MAX_DECOMPRESSED.
    """
    if name.endswith(".zst"):
        # stream_reader because Fedora's frames omit the content-size header.
        return _read_bounded(zstandard.ZstdDecompressor().stream_reader(data), limit, name)
    if name.endswith(".xz"):
        return _read_bounded(lzma.LZMAFile(io.BytesIO(data)), limit, name)
    if name.endswith(".gz"):
        return _read_bounded(gzip.GzipFile(fileobj=io.BytesIO(data)), limit, name)
    return data
