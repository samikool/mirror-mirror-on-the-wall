"""The typed disk operations a `DiskJob` routes. Each is one item's work,
safe on any thread."""
import gzip
import hashlib
import os
import shutil
from pathlib import Path


def stage_path(tmp_dir: Path, dst: Path) -> Path:
    """A collision-free staging name for `dst` inside `tmp_dir`.

    Hashed rather than derived by substitution, for the same reason
    `resources/apply.py:_tmp_path` hashes: a repo path can be arbitrarily
    long and deep, and flattening it into one directory by replacing "/"
    risks blowing a filesystem's max filename length.
    """
    digest = hashlib.sha256(str(dst).encode("utf-8", "surrogateescape")).hexdigest()
    return tmp_dir / digest


def atomic_write(dst: Path, data: bytes, tmp_dir: Path | None = None) -> None:
    """Write `data` at `dst` so a reader never sees a partial file.

    Same discipline as `hardlink_or_copy` below and for the same reason — a
    published tree is live-served with `autoindex on`.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if tmp_dir is None:
        tmp_dir = dst.parent
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = stage_path(tmp_dir, dst)
    try:
        tmp.write_bytes(data)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def write_if_changed(dst: Path, data: bytes) -> bool:
    """`atomic_write` unless `dst` already holds exactly `data` — an
    unchanged page keeps its mtime. True when written."""
    dst = Path(dst)
    try:
        if dst.read_bytes() == data:
            return False
    except OSError:
        pass
    atomic_write(dst, data)
    return True


def same_file(a: Path, b: Path) -> bool:
    """One inode — the device included. Inode numbers are per filesystem, so
    on a destination that falls back to copying (another device) an inode
    number alone could match the pool's by coincidence and skip a file that
    needed replacing."""
    sa, sb = a.stat(), b.stat()
    return sa.st_ino == sb.st_ino and sa.st_dev == sb.st_dev


def hardlink_or_copy(src: Path, dst: Path, tmp_dir: Path | None = None) -> None:
    """Hardlink src → dst, falling back to copy on cross-device — atomically.

    The whole pkg → meta → release ordering this module is built around
    exists so a client mid-`apt update` never sees an inconsistent tree. That
    guarantee was undercut one file at a time: replacing a CHANGED file used
    to be `dst.unlink()` then `dst.hardlink_to(src)`, so every such file had a
    window in which it 404'd, and the `shutil.copy2` fallback wrote in place,
    so on a cross-device destination it had a window in which it was
    *truncated and servable* — a client could fetch half a Packages.gz and
    cache it. Ordering the file classes is pointless if each individual file
    is non-atomic.

    So the new content is built at a staging path and `os.replace`d onto the
    destination: `dst` only ever transitions old-content → new-content, never
    through absent or partial. `tmp_dir` should be a directory on the
    destination's own filesystem that is never served — `TreeSink` passes
    `{root}/.mmw/tmp/`, the same place `apply.py` stages on the far side, and
    for the same reason (`{dst}.mmw-tmp` beside the real file was listed and
    fetchable under `autoindex on`).
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        try:
            if same_file(dst, src):
                return
        except OSError:
            pass
    if tmp_dir is None:
        tmp_dir = dst.parent
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = stage_path(tmp_dir, dst)
    try:
        if tmp.exists():
            tmp.unlink()
        try:
            tmp.hardlink_to(src)
        except OSError:
            shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def place(src: Path, dst: Path, tmp_dir: Path) -> str:
    """`hardlink_or_copy` semantics: the same inode already there → skipped;
    otherwise staged in `tmp_dir`, then `os.replace`d onto `dst`."""
    if dst.exists() and same_file(dst, src):
        return "skipped"
    hardlink_or_copy(src, dst, tmp_dir=tmp_dir)
    return "linked"


def remove(path) -> int | None:
    """Unlink `path`; its size freed, or None when it was already gone."""
    try:
        size = os.lstat(path).st_size
        os.unlink(path)
    except FileNotFoundError:
        return None
    return size


def open_append(path, offset: int):
    """A resume file opened to continue at `offset` (> 0), or fresh."""
    return open(path, "ab" if offset else "wb")


def link_into(src, dst) -> bool:
    """Hardlink `src` at `dst`; False when `dst` already exists."""
    try:
        os.link(src, dst)
    except FileExistsError:
        return False
    return True


def disk_usage(path):
    """`shutil.disk_usage`: (total, used, free) bytes of the filesystem holding `path`."""
    return shutil.disk_usage(path)


def copy_file(src, dst) -> None:
    shutil.copyfile(src, dst)


def reap_older_than(directory, cutoff: float) -> None:
    """Remove the plain files in `directory` last modified before `cutoff`;
    anything that will not go, or a missing directory, is left alone."""
    try:
        entries = list(Path(directory).iterdir())
    except OSError:
        return
    for f in entries:
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


# ── plain path operations ────────────────────────────────────────────────────
# One-off calls outside any lane: exactly `Path(path).<name>(...)` (or the
# os / builtin / gzip call), here so every disk call the guard can name passes
# through this layer. In-place writes are for
# files no reader can see mid-write; anything served goes through
# `atomic_write`.

def _path_method(name: str):
    def op(path, *args, **kwargs):
        return getattr(Path(path), name)(*args, **kwargs)
    op.__name__ = op.__qualname__ = name
    op.__doc__ = f"`Path(path).{name}(...)`."
    return op


exists = _path_method("exists")
is_file = _path_method("is_file")
is_dir = _path_method("is_dir")
iterdir = _path_method("iterdir")
glob = _path_method("glob")
rglob = _path_method("rglob")
mkdir = _path_method("mkdir")
rmdir = _path_method("rmdir")
unlink = _path_method("unlink")
rename = _path_method("rename")
replace = _path_method("replace")
read_text = _path_method("read_text")
write_text = _path_method("write_text")
write_bytes = _path_method("write_bytes")
chmod = _path_method("chmod")
is_symlink = _path_method("is_symlink")
walk = os.walk
rmtree = shutil.rmtree
disk_usage = shutil.disk_usage
open_file = open
open_fd = os.open
fsync = os.fsync
gzip_open = gzip.open


def stat(path) -> os.stat_result:
    return os.stat(path)


def lstat(path) -> os.stat_result:
    return os.lstat(path)


def read_bytes(path) -> bytes:
    return Path(path).read_bytes()


def open_rb(path):
    return open(path, "rb")


CHUNK = 1 << 20


def iter_chunks(path, chunk: int = CHUNK):
    """The file's bytes, read sequentially in `chunk`-sized pieces."""
    with open_rb(path) as fh:
        while data := fh.read(chunk):
            yield data


def read_through(path, chunk: int, stop_event=None) -> bool:
    """Read the whole file into one reused buffer, unbuffered — only to pull
    it into the page cache. False when `stop_event` ended it early."""
    buf = bytearray(chunk)
    with open(path, "rb", buffering=0) as f:
        while True:
            if stop_event is not None and stop_event.is_set():
                return False
            if not f.readinto(buf):
                return True


def copy_stream(src, dst, progress=None, *, chunk: int = CHUNK) -> None:
    """Copy `src` to `dst` through one reused buffer, then fsync `dst`.
    `progress(done, total)` after every chunk."""
    total = os.stat(src).st_size
    done = 0
    buf = bytearray(chunk)
    view = memoryview(buf)
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while n := fin.readinto(buf):
            fout.write(view[:n])
            done += n
            if progress:
                progress(done, total)
        fout.flush()
        os.fsync(fout.fileno())


def hash_fh(fh, algo: str = "sha256", chunk: int = CHUNK) -> tuple[str, int]:
    """(hexdigest, bytes read) of an open binary file, read to its end."""
    h, n = hashlib.new(algo), 0
    while data := fh.read(chunk):
        h.update(data)
        n += len(data)
    return h.hexdigest(), n
