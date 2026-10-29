"""Real `.deb` files for tests, built in pure Python (no dpkg-deb)."""
import io
import lzma
import tarfile
import time
from pathlib import Path

import zstandard


def _tar(members: dict[str, bytes], compression: str) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.mode = len(data), 0, 0o644
            tar.addfile(info, io.BytesIO(data))
    data = raw.getvalue()
    if compression == "gz":
        import gzip
        return gzip.compress(data, mtime=0)
    if compression == "xz":
        return lzma.compress(data)
    if compression == "zst":
        return zstandard.ZstdCompressor().compress(data)
    raise ValueError(compression)


def _ar(members: list[tuple[str, bytes]]) -> bytes:
    out = bytearray(b"!<arch>\n")
    for name, data in members:
        header = f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n"
        out += header.encode() + data
        if len(data) % 2:
            out += b"\n"
    return bytes(out)


def make_deb(dir: Path, package: str, version: str, arch: str = "amd64", *,
             compression: str = "gz", extra: dict[str, str] | None = None,
             filename: str | None = None) -> Path:
    fields = {"Package": package, "Version": version, "Architecture": arch,
              "Maintainer": "Test <t@example.com>", "Section": "utils",
              "Description": f"{package} for tests\n test package", **(extra or {})}
    control = "".join(f"{k}: {v}\n" for k, v in fields.items()).encode()
    readme = f"{package} {version}\n".encode()
    deb = _ar([("debian-binary", b"2.0\n"),
               (f"control.tar.{compression}", _tar({"./control": control}, compression)),
               ("data.tar.gz", _tar({f"./usr/share/doc/{package}/README": readme}, "gz"))])
    dir.mkdir(parents=True, exist_ok=True)
    path = dir / (filename or f"{package}_{version}_{arch}.deb")
    path.write_bytes(deb)
    return path


def touch_old(path: Path, seconds: int = 3600) -> None:
    """Move a file's mtime back, so a rewrite in the same second still differs."""
    t = time.time() - seconds
    import os
    os.utime(path, (t, t))
