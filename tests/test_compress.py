import gzip
import lzma

import zstandard

from mmw.core.compress import decompress

DATA = b"the quick brown fox" * 100


def test_gz():
    assert decompress(gzip.compress(DATA), "Packages.gz") == DATA


def test_xz():
    assert decompress(lzma.compress(DATA), "Packages.xz") == DATA


def test_zst():
    compressed = zstandard.ZstdCompressor().compress(DATA)
    assert decompress(compressed, "primary.xml.zst") == DATA


def test_zst_no_content_size_header():
    # Fedora frames omit the content-size header — emulate with a stream writer
    import io
    buf = io.BytesIO()
    with zstandard.ZstdCompressor().stream_writer(buf, closefd=False) as w:
        w.write(DATA)
    assert decompress(buf.getvalue(), "primary.xml.zst") == DATA


def test_passthrough():
    assert decompress(DATA, "Packages") == DATA
