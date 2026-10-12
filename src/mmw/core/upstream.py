"""Files mirrored byte-for-byte at their own upstream path.

A vendor's install instructions fetch more than the repository: a signing key
(`curl https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor`),
a ready-made sources file (`…/stable/deb/nvidia-container-toolkit.list`), a
key on the repository's host but outside its directory
(`https://packages.microsoft.com/keys/microsoft.asc`). Each is just a file at
a URL. A source names them (`gpg_key_url`, `extra_files`); sync fetches them
here, and publish/bundle place each at `{host}/{path}` — so once DNS points at
the mirror, the vendor's commands work unchanged.

Stored per **repository** (`upstream/{source name}/{host}/{path}`), not per
key label: a network gets exactly the files of the repositories it publishes.
The pool records no such files, so the directory's contents are the fact
publish reads.
"""
import posixpath
import shutil
from pathlib import Path
from urllib.parse import urlparse

UPSTREAM_DIR = "upstream"

# A generous ceiling for "a file a vendor's instructions fetch" — keys and
# sources files are kilobytes, a keyring .deb a few more. It stops a mistyped
# URL (a whole ISO, an endless stream) from filling the pool's disk.
MAX_BYTES = 64 * 1024 * 1024


def upstream_rel(url: str) -> str | None:
    """`{host}/{path}` for a URL — where its copy is published. The host is
    mangled for a non-standard port exactly as `MirrorConfig.mirror_base`
    mangles it, so a file beside its repository lands in that repository's
    directory. None when there is no 1-to-1 path to give it: no file name, a
    query string, or a path that would escape the tree."""
    from mmw.core.downloader import is_safe_repo_path

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    if parsed.query or parsed.fragment:
        return None
    path = parsed.path.lstrip("/")
    if not path or path.endswith("/"):
        return None
    host = parsed.hostname
    if parsed.port:
        host = f"{host}_{parsed.port}"
    rel = f"{host}/{path}"
    if not is_safe_repo_path(rel) or posixpath.normpath(rel) != rel:
        return None
    return rel


def repository_name(mirror) -> str:
    """A readable name for a mirror's repository — the *source*, never the
    expanded mirror name, which carries suite, component and arch. Used for
    file names a person reads (a key's stem); `repository_group` is the one
    that must be unique."""
    from mmw.core.config import derive_name

    source = getattr(mirror, "source", "") or mirror.source_url
    return derive_name(source, provider=mirror.provider) if source else mirror.name


def repository_group(mirror) -> str:
    """The directory a mirror's repository files are filed under, unique per
    repository: its name plus a digest of its URI. Sync (which stores) and
    publish (which reads) must agree on it exactly.

    A derived name alone is not unique — NVIDIA's per-arch flat repositories
    (`…/hpc-sdk/ubuntu/amd64`, `…/arm64`) derived one — and two repositories
    sharing a directory each pruned the other's files and only the first one
    synced ever fetched its own."""
    import hashlib

    source = getattr(mirror, "source", "") or mirror.source_url
    if not source:
        return mirror.name
    return f"{repository_name(mirror)}-{hashlib.sha256(source.encode()).hexdigest()[:8]}"


def legacy_group(mirror) -> str | None:
    """Where a repository's files were filed before `repository_group` was
    unique — read by publish until the next sync refiles them."""
    from mmw.core.config import derive_name

    source = getattr(mirror, "source", "") or mirror.source_url
    return derive_name(source) if source else None


def group_dir(mirror_dir: Path, group: str) -> Path:
    return Path(mirror_dir) / UPSTREAM_DIR / group


def fetch_bytes(url: str, client) -> bytes:
    """The file exactly as the server sent it (redirects followed — the copy
    is still published at the URL the instructions name).

    Streamed against MAX_BYTES: reading `resp.content` first and checking
    after buffered the whole response, so the cap could not stop the
    endless stream it exists for."""
    with client.stream("GET", url, timeout=30, follow_redirects=True) as resp:
        resp.raise_for_status()
        data = bytearray()
        for chunk in resp.iter_bytes():
            data += chunk
            if len(data) > MAX_BYTES:
                raise ValueError(f"more than the {MAX_BYTES >> 20} MiB allowed for an "
                                 "upstream file")
    return bytes(data)


def store(mirror_dir: Path, group: str, url: str, raw: bytes) -> Path:
    """Keep `raw` as the copy of `url`. Written via rename, so a reader never
    sees a half-written file. Raises ValueError for a URL with no 1-to-1 path."""
    rel = upstream_rel(url)
    if rel is None:
        raise ValueError(f"{url} has no path to mirror it at")
    dest = group_dir(mirror_dir, group) / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.read_bytes() == raw:
        return dest
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(raw)
    tmp.replace(dest)
    return dest


def prune(mirror_dir: Path, group: str, urls: list[str]) -> list[str]:
    """Remove stored copies whose URL the source no longer names. A URL still
    named keeps its copy even if this sync's fetch failed — last good wins.
    Returns the removed `{host}/{path}`s."""
    base = group_dir(mirror_dir, group)
    if not base.is_dir() or base.is_symlink():
        return []
    wanted = {r for r in (upstream_rel(u) for u in urls) if r}
    removed: list[str] = []
    for f in sorted(base.rglob("*")):
        if f.is_file() and not f.is_symlink():
            rel = f.relative_to(base).as_posix()
            if rel not in wanted:
                f.unlink()
                removed.append(rel)
    for d in sorted((p for p in base.rglob("*") if p.is_dir()),
                    key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    if not any(base.iterdir()):
        shutil.rmtree(base, ignore_errors=True)
    return removed
