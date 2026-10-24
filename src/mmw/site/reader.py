"""A bundle's members, from wherever it arrives: a tar file, a tar streamed
on stdin (`cat lab.tar | ssh host mmw site apply -`), or a stage directory
(`mmw stage`). Members come in any order, with or without a leading `./`;
every name is checked before anything uses it, with the same rules as
`apply.py`."""
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from mmw.core import disk_io
from mmw.resources.apply import is_safe_path

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


class SourceError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class Member:
    name: str
    kind: str                    # "file" | "link"
    fh: BinaryIO | None          # a file's bytes, read before the next member
    linkname: str | None         # a link's target member
    size: int


def _name(raw: str) -> str:
    name = raw[2:] if raw.startswith("./") else raw
    if not is_safe_path(name):
        raise SourceError(2, f"refusing unsafe path in bundle: {raw!r}")
    return name


def _from_tar(tar: tarfile.TarFile) -> Iterator[Member]:
    with tar:
        try:
            yield from _members(tar)
        except (tarfile.ReadError, EOFError) as e:
            raise SourceError(5, f"bundle is truncated ({e}) — copy it again") from None


def _members(tar: tarfile.TarFile) -> Iterator[Member]:
    for m in tar:
        if m.isdir() or m.name in (".", "./"):
            continue
        name = _name(m.name)
        if m.isreg():
            yield Member(name, "file", tar.extractfile(m), None, m.size)
        elif m.islnk():
            yield Member(name, "link", None, _name(m.linkname), 0)
        else:
            raise SourceError(2, f"refusing {name!r}: only files and hardlinks belong in a bundle")


def _from_dir(root: Path) -> Iterator[Member]:
    for dirpath, dirnames, filenames in disk_io.ops.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            path = Path(dirpath) / fn
            name = _name(path.relative_to(root).as_posix())
            if disk_io.ops.is_symlink(path):
                raise SourceError(2, f"refusing {name!r}: a symlink in a stage directory")
            with disk_io.ops.open_rb(path) as fh:
                yield Member(name, "file", fh, None, disk_io.ops.stat(path).st_size)


def open_source(src: str) -> Iterator[Member]:
    """The members of the bundle at `src` ("-": stdin)."""
    if src == "-":
        yield from _from_tar(tarfile.open(fileobj=sys.stdin.buffer, mode="r|"))
        return
    path = Path(src)
    if disk_io.ops.is_dir(path):
        yield from _from_dir(path)
        return
    with disk_io.ops.open_rb(path) as fh:
        if fh.read(4) == _ZSTD_MAGIC:
            raise SourceError(5, f"{path} is zstd-compressed — decompress it first (zstd -d {path})")
    try:
        tar = tarfile.open(path, mode="r|")
    except tarfile.ReadError as e:
        raise SourceError(5, f"{path} is not a tar, or is truncated: {e} — copy it again") from None
    yield from _from_tar(tar)
