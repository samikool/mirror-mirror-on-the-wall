"""A root's blob store: `{root}/.mmw/store/{cs[:2]}/{cs}`, one file per
distinct content. Every file the root serves is a hardlink to one of these,
so serving another version is relinking, never copying. `{root}/.mmw/` is in
no nginx `root`, so nothing here is ever served."""
import hashlib
import os
from pathlib import Path
from typing import BinaryIO

from mmw.core import disk_io

_CHUNK = 1024 * 1024


_SHA256 = __import__("re").compile(r"[0-9a-f]{64}")


class ChecksumMismatch(ValueError):
    """Bytes that don't hash to what the manifest says: damaged on the way."""


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir = self.root / ".mmw" / "store"
        self.tmp = self.dir / ".tmp"
        self.place_tmp = self.root / ".mmw" / "tmp"

    def path(self, checksum: str) -> Path:
        if not _SHA256.fullmatch(checksum or ""):
            raise ValueError(f"{checksum[:80]!r} isn't a sha256 — refusing it as a store path")
        return self.dir / checksum[:2] / checksum

    def has(self, checksum: str) -> bool:
        return disk_io.ops.exists(self.path(checksum))

    def add_stream(self, fh: BinaryIO, expect: str | None = None) -> tuple[str, int]:
        """Store `fh`'s bytes; (checksum, size). With `expect`, bytes that do
        not hash to it are refused and nothing is kept — and content the
        store already has is only hashed to check it, never written again."""
        if expect is not None and self.has(expect):
            checksum, size = disk_io.ops.hash_fh(fh)
            if checksum != expect:
                raise ChecksumMismatch(f"content hashes to {checksum[:12]}…, expected {expect[:12]}…")
            return checksum, size
        disk_io.ops.mkdir(self.tmp, parents=True, exist_ok=True)
        tmp = self.tmp / f"in-{os.getpid()}-{id(fh)}"
        h, size = hashlib.sha256(), 0
        try:
            with disk_io.ops.open_file(tmp, "wb") as out:
                while chunk := fh.read(_CHUNK):
                    h.update(chunk)
                    out.write(chunk)
                    size += len(chunk)
            checksum = h.hexdigest()
            if expect is not None and checksum != expect:
                raise ChecksumMismatch(f"content hashes to {checksum[:12]}…, expected {expect[:12]}…")
            dest = self.path(checksum)
            if disk_io.ops.exists(dest):
                return checksum, size
            disk_io.ops.mkdir(dest.parent, parents=True, exist_ok=True)
            disk_io.ops.replace(tmp, dest)
            return checksum, size
        finally:
            disk_io.ops.unlink(tmp, missing_ok=True)

    def place(self, checksum: str, dest: Path) -> None:
        """Make `dest` this blob (hardlink, staged and renamed in)."""
        disk_io.ops.mkdir(Path(dest).parent, parents=True, exist_ok=True)
        disk_io.ops.place(self.path(checksum), Path(dest), self.place_tmp)

    def gc(self, keep: set[str]) -> tuple[int, int]:
        """Remove every blob not in `keep`; (files, bytes) freed."""
        files = freed = 0
        if not disk_io.ops.exists(self.dir):
            return 0, 0
        for sub in disk_io.ops.iterdir(self.dir):
            if sub.name == ".tmp" or not disk_io.ops.is_dir(sub):
                continue
            for blob in disk_io.ops.iterdir(sub):
                if blob.name not in keep:
                    size = disk_io.ops.remove(blob)
                    if size is not None:
                        files, freed = files + 1, freed + size
        return files, freed

    def sweep_tmp(self) -> None:
        for d in (self.tmp, self.place_tmp):
            if disk_io.ops.exists(d):
                for entry in disk_io.ops.iterdir(d):
                    disk_io.ops.remove(entry)
