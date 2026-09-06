import gzip
import lzma

import zstandard


def decompress(data: bytes, name: str) -> bytes:
    """Decompress bytes based on file extension in name (URL or filename).

    Handles .zst / .xz / .gz; returns data unchanged for any other suffix.
    """
    if name.endswith(".zst"):
        return zstandard.ZstdDecompressor().stream_reader(data).read()
    if name.endswith(".xz"):
        return lzma.decompress(data)
    if name.endswith(".gz"):
        return gzip.decompress(data)
    return data
