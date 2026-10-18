"""A fake pypi.org / files.pythonhosted.org behind httpx.MockTransport."""
import hashlib
from urllib.parse import urlsplit

import httpx


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fake_body(filename: str) -> bytes:
    """Deterministic stand-in for a distribution file: its own name."""
    return f"bytes of {filename}".encode()


def file_entry(filename: str, *, requires_python=None, yanked=False, metadata: bytes | None = None,
               body: bytes | None = None) -> dict:
    """One PEP 691/700 file entry; the fake serves `body` and `metadata` for it."""
    body = fake_body(filename) if body is None else body
    digest = sha(body)
    entry = {
        "filename": filename,
        "url": f"https://files.pythonhosted.org/packages/{digest[:2]}/{digest[2:4]}/{digest[4:]}/{filename}",
        "hashes": {"sha256": digest}, "size": len(body), "requires-python": requires_python,
        "yanked": yanked, "upload-time": "2026-01-01T00:00:00Z",
        "core-metadata": {"sha256": sha(metadata)} if metadata is not None else False,
        "data-dist-info-metadata": {"sha256": sha(metadata)} if metadata is not None else False,
        "_body": body, "_metadata": metadata,
    }
    return entry


def metadata_text(name: str, version: str, requires=(), extras=()) -> bytes:
    lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    lines += [f"Provides-Extra: {e}" for e in extras]
    lines += [f"Requires-Dist: {r}" for r in requires]
    return ("\n".join(lines) + "\n").encode()


class FakePyPI:
    def __init__(self):
        self.projects: dict[str, dict] = {}     # canonical name -> {"serial": int, "files": [entries]}
        self.requests: list[httpx.Request] = []
        self.status: dict[str, int] = {}        # project name -> forced status for its page
        self.stop_after: tuple | None = None    # (threading.Event, n project requests)
        self.bad_metadata: set[str] = set()     # file urls whose .metadata body is corrupted

    def add(self, name: str, files: list[dict], serial: int = 1) -> None:
        self.projects[name] = {"serial": serial, "files": files}

    def _by_url(self) -> dict[str, dict]:
        return {f["url"]: f for p in self.projects.values() for f in p["files"]}

    def project_requests(self) -> list[str]:
        return [urlsplit(str(r.url)).path.split("/")[2] for r in self.requests
                if urlsplit(str(r.url)).path.startswith("/simple/") and urlsplit(str(r.url)).path != "/simple/"]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        path = urlsplit(url).path
        if path == "/simple/":
            return httpx.Response(200, json={"meta": {"api-version": "1.4"}, "projects": [
                {"name": n, "_last-serial": p["serial"]} for n, p in self.projects.items()]})
        if path.startswith("/simple/"):
            name = path.split("/")[2]
            if self.stop_after is not None:
                event, n = self.stop_after
                if len(self.project_requests()) >= n:
                    event.set()
            if name in self.status:
                return httpx.Response(self.status[name])
            if name not in self.projects:
                return httpx.Response(404)
            files = [{k: v for k, v in f.items() if not k.startswith("_")}
                     for f in self.projects[name]["files"]]
            return httpx.Response(200, json={"meta": {"api-version": "1.4", "_last-serial": self.projects[name]["serial"]},
                                             "name": name, "files": files})
        by_url = self._by_url()
        if url.endswith(".metadata") and url[:-len(".metadata")] in by_url:
            body = by_url[url[:-len(".metadata")]]["_metadata"]
            if body is None:
                return httpx.Response(404)
            return httpx.Response(200, content=b"corrupt" if url[:-9] in self.bad_metadata else body)
        if url in by_url:
            return httpx.Response(200, content=by_url[url]["_body"])
        return httpx.Response(404)

    def client(self, *args, **kwargs) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(self))


# ── a dict-backed Universe for closure tests ────────────────────────────────

def dist(name, version, tag="py3-none-any", *, requires=(), extras=(), requires_python=None,
         yanked=False, kind="wheel"):
    """(DistFile, (requires, extras)) for one file of a release."""
    from packaging.utils import canonicalize_name

    from mmw.providers.pypi.index import DistFile
    stem = canonicalize_name(name).replace("-", "_")
    filename = f"{stem}-{version}-{tag}.whl" if kind == "wheel" else f"{stem}-{version}.tar.gz"
    digest = sha(fake_body(filename))
    f = DistFile(project=canonicalize_name(name), filename=filename,
                 url=f"https://files.pythonhosted.org/packages/{digest[:2]}/{digest[2:4]}/{digest[4:]}/{filename}",
                 sha256=digest, size=len(filename), version=version, kind=kind,
                 requires_python=requires_python, yanked=yanked,
                 metadata_sha256=sha(filename.encode()) if kind == "wheel" else None)
    return f, (list(requires), list(extras))


class FakeUniverse:
    def __init__(self, *dists):
        self.by_name: dict[str, list] = {}
        self.meta: dict[str, tuple] = {}
        for f, meta in dists:
            self.by_name.setdefault(f.project, []).append(f)
            self.meta[f.filename] = meta
        self.prefetched: list[list[str]] = []
        self.files_calls: list[str] = []

    def names(self):
        return sorted(self.by_name)

    def files(self, name):
        self.files_calls.append(name)
        return list(self.by_name.get(name, []))

    def scan(self):
        for name in reversed(sorted(self.by_name)):    # any order but name order
            yield name, list(self.by_name[name])

    def prefetch(self, files):
        self.prefetched.append(sorted({f.filename for f in files}))

    def metadata(self, file):
        return self.meta.get(file.filename) if file.kind == "wheel" else None


# ── recorded fixtures ───────────────────────────────────────────────────────

def from_fixtures() -> FakePyPI:
    """A FakePyPI serving the recorded pages (tests/fixtures/pypi). Each file's
    body is a stand-in whose sha256 and size replace the recorded ones; core
    metadata is served as recorded (its hash already matches the cut body)."""
    import json
    from pathlib import Path
    fix = Path(__file__).parent / "fixtures" / "pypi"
    fake = FakePyPI()
    for page in sorted((fix / "projects").glob("*.json")):
        data = json.loads(page.read_text())
        files = []
        for f in data["files"]:
            body = fake_body(f["filename"])
            meta = f.get("core-metadata")
            metadata = ((fix / "metadata" / f"{meta['sha256']}.metadata").read_bytes()
                        if isinstance(meta, dict) else None)
            files.append({**f, "hashes": {"sha256": sha(body)}, "size": len(body),
                          "data-dist-info-metadata": meta, "_body": body, "_metadata": metadata})
        fake.add(data["name"], files, serial=data["serial"])
    return fake
