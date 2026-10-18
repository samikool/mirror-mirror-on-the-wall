"""pypi.org's index, cached incrementally under the mirror.

The PEP 691 root listing carries each project's `_last-serial`; a project
page is refetched only when its serial moved. Pages are committed a batch at
a time, so a stopped first sync (about 917k pages) keeps what it read and the
next run continues from there. PEP 658 core metadata is cached by its sha256.
"""
import json
import posixpath
import sqlite3
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from packaging.tags import Tag
from packaging.utils import (InvalidSdistFilename, InvalidWheelFilename, canonicalize_name,
                             parse_sdist_filename, parse_wheel_filename)
from packaging.version import InvalidVersion

from mmw.core.http import retrying_client

SIMPLE_URL = "https://pypi.org/simple/"
ACCEPT = "application/vnd.pypi.simple.v1+json"
CACHE_DIR = "pypi-cache"   # under mirror_dir
FILES_HOST = "files.pythonhosted.org"
USER_AGENT = "mmw (+https://github.com/samikool/mirror-mirror-on-the-wall)"


def _client() -> httpx.Client:
    return retrying_client(timeout=60, follow_redirects=True, headers={"User-Agent": USER_AGENT})


@dataclass(frozen=True)
class DistFile:
    project: str            # canonical name
    filename: str
    url: str
    sha256: str
    size: int
    version: str
    kind: str               # "wheel" | "sdist"
    requires_python: str | None
    yanked: bool | str               # True, or PyPI's reason string
    metadata_sha256: str | None
    upload_time: str | None = None   # PEP 700

    @property
    def repo_path(self) -> str:
        return "files.pythonhosted.org/" + urlsplit(self.url).path.lstrip("/")

    @property
    def wheel_tags(self) -> frozenset[Tag]:
        return _wheel_tags(self.filename) if self.kind == "wheel" else frozenset()

    def _row(self) -> list:
        return [self.filename, self.url, self.sha256, self.size, self.version, self.kind,
                self.requires_python, self.yanked, self.metadata_sha256, self.upload_time]


@lru_cache(maxsize=1 << 18)
def _wheel_tags(filename: str) -> frozenset[Tag]:
    # By name, not per object: the closure rebuilds DistFiles from the cache
    # on every lookup, and parsing tags dominated its time.
    return parse_wheel_filename(filename)[3]


def _yanked(value) -> bool | str:
    """PEP 592/691: false, true, or a reason string (which means yanked)."""
    if isinstance(value, str):
        return value or True
    return bool(value)


def _metadata_hash(entry: dict) -> str | None:
    for key in ("core-metadata", "data-dist-info-metadata"):
        value = entry.get(key)
        if isinstance(value, dict) and value.get("sha256"):
            return value["sha256"]
    return None


def _file_url_ok(url: str) -> bool:
    """Files are served at their upstream path, so only a clean
    files.pythonhosted.org path is accepted: anything else would alias another
    host into this tree, or be refused later and sink the unit's commit."""
    from mmw.core.downloader import is_safe_repo_path   # downloader imports the providers
    parts = urlsplit(url)
    path = parts.path.lstrip("/")
    return (parts.scheme == "https" and parts.hostname == FILES_HOST and path.startswith("packages/")
            and posixpath.normpath(path) == path and is_safe_repo_path(path))


def _parse_entry(project: str, entry: dict) -> DistFile | None:
    filename = entry.get("filename", "")
    digest = (entry.get("hashes") or {}).get("sha256")
    if filename.endswith(".whl"):
        version, kind = parse_wheel_filename(filename)[1], "wheel"
    elif filename.endswith((".tar.gz", ".zip")):
        version, kind = parse_sdist_filename(filename)[1], "sdist"
    else:
        return None
    if not digest or not isinstance(entry.get("url"), str) or not _file_url_ok(entry["url"]):
        return None
    return DistFile(
        project=project, filename=filename, url=entry["url"], sha256=digest,
        size=int(entry.get("size") or 0), version=str(version), kind=kind,
        requires_python=entry.get("requires-python") or None,
        yanked=_yanked(entry.get("yanked")), metadata_sha256=_metadata_hash(entry),
        upload_time=entry.get("upload-time") or None)


def parse_project(name: str, body: dict) -> tuple[list[DistFile], int]:
    """Wheels and sdists from one PEP 691 project page, and how many entries
    were skipped (eggs, installers, names or versions that do not parse,
    malformed entries, URLs off files.pythonhosted.org). A bad entry costs
    that entry, never the project or the resolve."""
    if not isinstance(body, dict) or not isinstance(body.get("files", []), list):
        return [], 0
    project = canonicalize_name(name)
    files, skipped = [], 0
    for entry in body.get("files", []):
        try:
            f = _parse_entry(project, entry)
        except (InvalidWheelFilename, InvalidSdistFilename, InvalidVersion,
                AttributeError, TypeError, ValueError):
            f = None
        if f is None:
            skipped += 1
        else:
            files.append(f)
    return files, skipped


class IndexCache:
    """`{mirror_dir}/pypi-cache/index.sqlite`, or in memory without a mirror."""

    def __init__(self, mirror_dir: Path | None):
        if mirror_dir is None:
            target = ":memory:"
        else:
            (Path(mirror_dir) / CACHE_DIR).mkdir(parents=True, exist_ok=True)
            target = str(Path(mirror_dir) / CACHE_DIR / "index.sqlite")
        self._lock = threading.Lock()
        self._db = sqlite3.connect(target, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            "CREATE TABLE IF NOT EXISTS projects (name TEXT PRIMARY KEY, serial INTEGER, files BLOB);"
            "CREATE TABLE IF NOT EXISTS metadata (sha256 TEXT PRIMARY KEY, body BLOB);"
            "CREATE TABLE IF NOT EXISTS build_requires (sha256 TEXT PRIMARY KEY, requires TEXT);")

    def serials(self) -> dict[str, int]:
        with self._lock:
            return dict(self._db.execute("SELECT name, serial FROM projects"))

    def put_projects(self, rows: list[tuple[str, int, list[DistFile]]]) -> None:
        packed = [(name, serial, zlib.compress(json.dumps([f._row() for f in files],
                                                          separators=(",", ":")).encode()))
                  for name, serial, files in rows]
        with self._lock, self._db:
            self._db.executemany("INSERT OR REPLACE INTO projects VALUES (?, ?, ?)", packed)

    def drop_projects(self, names: list[str]) -> None:
        with self._lock, self._db:
            self._db.executemany("DELETE FROM projects WHERE name = ?", [(n,) for n in names])

    def files(self, name: str) -> list[DistFile]:
        with self._lock:
            row = self._db.execute("SELECT files FROM projects WHERE name = ?", (name,)).fetchone()
        return [] if row is None else self._decode(name, row[0])

    @staticmethod
    def _decode(name: str, blob: bytes) -> list[DistFile]:
        return [DistFile(name, *r) for r in json.loads(zlib.decompress(blob))]

    def scan(self, batch: int = 500):
        """Every project with its files, in storage order: one front-to-back
        pass over the file instead of a seek per name — on a spinning disk the
        difference between minutes and hours for all of PyPI. Keyset-paged on
        rowid, so no cursor stays open across the caller's other queries."""
        last = 0
        while True:
            with self._lock:
                rows = self._db.execute(
                    "SELECT rowid, name, files FROM projects WHERE rowid > ? ORDER BY rowid LIMIT ?",
                    (last, batch)).fetchall()
            if not rows:
                return
            for _rowid, name, blob in rows:
                yield name, self._decode(name, blob)
            last = rows[-1][0]

    def names(self) -> list[str]:
        with self._lock:
            return [n for (n,) in self._db.execute("SELECT name FROM projects ORDER BY name")]

    def metadata(self, sha: str) -> bytes | None:
        with self._lock:
            row = self._db.execute("SELECT body FROM metadata WHERE sha256 = ?", (sha,)).fetchone()
        return row[0] if row else None

    def missing_metadata(self, shas) -> set[str]:
        """Which of `shas` the cache does not hold — by key, never reading a body."""
        wanted = list(dict.fromkeys(s for s in shas if s))
        have: set[str] = set()
        with self._lock:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start:start + 500]
                have.update(r for (r,) in self._db.execute(
                    f"SELECT sha256 FROM metadata WHERE sha256 IN ({','.join('?' * len(chunk))})", chunk))
        return set(wanted) - have

    def put_metadata(self, rows: list[tuple[str, bytes]]) -> None:
        with self._lock, self._db:
            self._db.executemany("INSERT OR REPLACE INTO metadata VALUES (?, ?)", rows)

    def build_requires(self, sha: str):
        """An sdist's cached `BuildRequires`, or None."""
        from mmw.providers.pypi.build import BuildRequires
        with self._lock:
            row = self._db.execute("SELECT requires FROM build_requires WHERE sha256 = ?", (sha,)).fetchone()
        return None if row is None else BuildRequires(**json.loads(row[0]))

    def put_build_requires(self, sha: str, found) -> None:
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO build_requires VALUES (?, ?)",
                             (sha, json.dumps({"requires": found.requires, "note": found.note})))

    def close(self) -> None:
        with self._lock:
            self._db.close()


@dataclass
class RefreshResult:
    fetched: int = 0
    removed: int = 0
    failed: list[str] = field(default_factory=list)
    skipped_files: int = 0
    stopped: bool = False


def _fetch_page(client: httpx.Client, name: str):
    """(files, skipped) for a project page; ([], 0) for one that is gone."""
    resp = client.get(f"{SIMPLE_URL}{name}/", headers={"Accept": ACCEPT})
    if resp.status_code == 404:
        return [], 0
    resp.raise_for_status()
    return parse_project(name, resp.json())


def refresh(cache: IndexCache, client_factory, *, threads: int, say=None, stop_event=None,
            batch: int = 1000) -> RefreshResult:
    """Bring the cache up to PyPI's current serials. Unchanged projects are
    not requested; a failed page keeps its previous row (and old serial, so
    the next refresh retries it)."""
    result = RefreshResult()
    with client_factory() as client:
        resp = client.get(SIMPLE_URL, headers={"Accept": ACCEPT})
        resp.raise_for_status()
        listed = {canonicalize_name(p["name"]): int(p.get("_last-serial") or 0)
                  for p in resp.json().get("projects", [])}
        have = cache.serials()
        gone = [n for n in have if n not in listed]
        if gone:
            cache.drop_projects(gone)
            result.removed = len(gone)
        todo = sorted(n for n, serial in listed.items() if have.get(n) != serial)

        def one(name):
            try:
                return name, _fetch_page(client, name), None
            except (httpx.HTTPError, ValueError) as e:
                return name, None, e

        with ThreadPoolExecutor(max(1, threads)) as pool:
            for start in range(0, len(todo), batch):
                if stop_event is not None and stop_event.is_set():
                    result.stopped = True
                    break
                rows = []
                for name, page, error in pool.map(one, todo[start:start + batch]):
                    if error is not None:
                        result.failed.append(name)
                        continue
                    files, skipped = page
                    result.skipped_files += skipped
                    rows.append((name, listed[name], files))
                cache.put_projects(rows)
                result.fetched += len(rows)
                if say:
                    say(f"PyPI index: {start + len(todo[start:start + batch])}/{len(todo)} changed projects")
    return result


def refresh_projects(cache: IndexCache, names, client_factory, *, threads: int,
                     stop_event=None) -> RefreshResult:
    """Fetch the named project pages now, whatever their cached serial — the
    requirements flow needs only these, current with what uv just resolved,
    and never the 917k-project listing."""
    result = RefreshResult()
    names = sorted(set(names))
    with client_factory() as client:

        def one(name):
            if stop_event is not None and stop_event.is_set():
                return name, None, "stopped"
            try:
                resp = client.get(f"{SIMPLE_URL}{name}/", headers={"Accept": ACCEPT})
                if resp.status_code == 404:
                    return name, ([], 0, 0), None
                resp.raise_for_status()
                body = resp.json()
                serial = int(((body.get("meta") or {}) if isinstance(body, dict) else {}).get("_last-serial") or 0)
                return name, (*parse_project(name, body), serial), None
            except (httpx.HTTPError, ValueError) as e:
                return name, None, e

        rows = []
        with ThreadPoolExecutor(max(1, threads)) as pool:
            for name, page, error in pool.map(one, names):
                if error is not None:
                    result.failed.append(name)
                    result.stopped = result.stopped or error == "stopped"
                    continue
                files, skipped, serial = page
                result.skipped_files += skipped
                rows.append((name, serial, files))
        cache.put_projects(rows)
        result.fetched = len(rows)
    return result


def fetch_metadata(cache: IndexCache, files: list[DistFile], client_factory, *, threads: int,
                   say=None, stop_event=None) -> list[str]:
    """Cache the PEP 658 core metadata of `files` (one request per distinct,
    not-yet-cached hash). Returns one line per failure."""
    missing = cache.missing_metadata(f.metadata_sha256 for f in files)
    wanted: dict[str, DistFile] = {}
    for f in files:
        if f.metadata_sha256 in missing and f.metadata_sha256 not in wanted:
            wanted[f.metadata_sha256] = f
    failures: list[str] = []
    if not wanted:
        return failures
    with client_factory() as client:

        def one(f: DistFile):
            if stop_event is not None and stop_event.is_set():
                return f, None, "stopped"
            try:
                resp = client.get(f.url + ".metadata")
                resp.raise_for_status()
            except httpx.HTTPError as e:
                return f, None, str(e)
            if sha256(resp.content).hexdigest() != f.metadata_sha256:
                return f, None, "core metadata does not match its sha256"
            return f, resp.content, None

        rows = []
        with ThreadPoolExecutor(max(1, threads)) as pool:
            for f, body, error in pool.map(one, list(wanted.values())):
                if error is not None:
                    failures.append(f"{f.filename}: {error}")
                    continue
                rows.append((f.metadata_sha256, body))
                if len(rows) >= 1000:
                    cache.put_metadata(rows)
                    rows = []
        cache.put_metadata(rows)
    if say:
        say(f"PyPI metadata: {len(wanted) - len(failures)} fetched")
    return failures
