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


def test_decompress_refuses_a_decompression_bomb():
    """Every caller feeds `decompress` bytes from an upstream repository, so an
    unbounded expansion is a denial of service any mirrored repo could inflict
    — a ~1 MB index expanding to tens of GB took the whole sync process with
    it (and, in the TUI, the app)."""
    import lzma

    import pytest

    from mmw.core.compress import DecompressionTooLarge, decompress

    bomb = lzma.compress(b"\0" * (4 << 20))          # 4 MiB of zeros, tiny compressed
    assert len(bomb) < 10_000                        # genuinely expansive
    with pytest.raises(DecompressionTooLarge):
        decompress(bomb, "Packages.xz", limit=1 << 20)
    # ...and is returned intact when it fits.
    assert len(decompress(bomb, "Packages.xz")) == 4 << 20
