"""Where a bundle's members go: one plain tar on a file object, or the same
members laid out as a directory (`mmw stage`).

`BundleSink` decides what goes in and in what order; a writer only puts
members somewhere. Neither is final until `commit()`: a tar is renamed
into place (or, on stdout, flushed), a directory renamed from its `.tmp`
name. `abort()` leaves nothing a later step could mistake for a whole
bundle.
"""
import hashlib
import io
import tarfile
from pathlib import Path
from typing import BinaryIO, Protocol

from mmw.core import disk_io

_COPY_CHUNK = 1024 * 1024


class MemberWriter(Protocol):
    def add_bytes(self, name: str, data: bytes, mode: int = 0o644) -> None: ...
    def add_blob(self, name: str, fh: BinaryIO, size: int, src: Path | None = None) -> None: ...
    def add_hardlink(self, name: str, target: str) -> None: ...
    def commit(self) -> None: ...
    def abort(self) -> None: ...


class TarMembers:
    """A streaming (`w|`) tar over `fileobj`. With `final_path`/`tmp_path`,
    `fileobj` is the open tmp file: commit renames it into place, abort
    unlinks it. Without them (stdout), commit only flushes."""

    def __init__(self, fileobj: BinaryIO, *, final_path: Path | None = None,
                 tmp_path: Path | None = None, copybufsize: int = _COPY_CHUNK):
        self._fh = fileobj
        self._final, self._tmp = final_path, tmp_path
        self._tar = tarfile.open(fileobj=fileobj, mode="w|", copybufsize=copybufsize)

    def add_bytes(self, name: str, data: bytes, mode: int = 0o644) -> None:
        ti = tarfile.TarInfo(name)
        ti.size = len(data)
        ti.mode = mode
        self._tar.addfile(ti, io.BytesIO(data))

    def add_blob(self, name: str, fh: BinaryIO, size: int, src: Path | None = None) -> None:
        ti = tarfile.TarInfo(name)
        ti.size = size
        ti.mode = 0o644
        self._tar.addfile(ti, fh)

    def add_hardlink(self, name: str, target: str) -> None:
        ti = tarfile.TarInfo(name)
        ti.type = tarfile.LNKTYPE
        ti.linkname = target
        ti.mode = 0o644
        self._tar.addfile(ti)

    def commit(self) -> None:
        self._tar.close()
        self._fh.flush()
        if self._tmp is not None:
            self._fh.close()
            disk_io.ops.replace(self._tmp, self._final)

    def abort(self) -> None:
        try:
            self._tar.close()
        except Exception:                # noqa: BLE001 — already failing; leave nothing behind
            pass
        if self._tmp is not None:
            try:
                self._fh.close()
            finally:
                disk_io.ops.unlink(self._tmp, missing_ok=True)


class DirMembers:
    """The members as files under `final_dir`, written to `{final_dir}.tmp`
    and renamed on commit. A blob is hardlinked from its pool file when it
    can be; otherwise copied from the stream, counted in `copied_bytes`."""

    def __init__(self, final_dir: Path):
        self.final_dir = final_dir
        self.tmp_dir = final_dir.with_name(final_dir.name + ".tmp")
        self.copied_bytes = 0
        self.copied_files = 0
        if disk_io.ops.exists(self.tmp_dir):            # debris of a killed stage
            disk_io.ops.rmtree(self.tmp_dir)
        disk_io.ops.mkdir(self.tmp_dir, parents=True)

    def _dest(self, name: str) -> Path:
        dest = self.tmp_dir / name
        disk_io.ops.mkdir(dest.parent, parents=True, exist_ok=True)
        return dest

    def add_bytes(self, name: str, data: bytes, mode: int = 0o644) -> None:
        dest = self._dest(name)
        disk_io.ops.write_bytes(dest, data)
        disk_io.ops.chmod(dest, mode)

    def _copy(self, fh: BinaryIO, dest: Path) -> int:
        n = 0
        with disk_io.ops.open_file(dest, "wb") as out:
            while chunk := fh.read(_COPY_CHUNK):
                out.write(chunk)
                n += len(chunk)
        return n

    def add_blob(self, name: str, fh: BinaryIO, size: int, src: Path | None = None) -> None:
        dest = self._dest(name)
        if src is not None:
            try:
                if disk_io.ops.link_into(src, dest):
                    return
            except OSError:                     # another filesystem, or links refused
                pass
        self.copied_bytes += self._copy(fh, dest)
        self.copied_files += 1

    def add_hardlink(self, name: str, target: str) -> None:
        dest, first = self._dest(name), self.tmp_dir / target
        try:
            if disk_io.ops.link_into(first, dest):
                return
        except OSError:
            pass
        with disk_io.ops.open_file(first, "rb") as fh:
            self.copied_bytes += self._copy(fh, dest)
        self.copied_files += 1

    def commit(self) -> None:
        disk_io.ops.replace(self.tmp_dir, self.final_dir)

    def abort(self) -> None:
        if disk_io.ops.exists(self.tmp_dir):
            disk_io.ops.rmtree(self.tmp_dir)


class HashingWriter(io.RawIOBase):
    """A pass-through to `fileobj` that hashes and counts what goes by."""

    def __init__(self, fileobj: BinaryIO):
        self._out = fileobj
        self._h = hashlib.sha256()
        self.written = 0

    def writable(self) -> bool:
        return True

    def write(self, b) -> int:
        n = self._out.write(b)
        n = len(b) if n is None else n
        self._h.update(memoryview(b)[:n])
        self.written += n
        return n

    def flush(self) -> None:
        self._out.flush()

    def hexdigest(self) -> str:
        return self._h.hexdigest()
