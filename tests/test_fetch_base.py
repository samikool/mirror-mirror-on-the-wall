"""A unit can fetch from another base (an archive) while keeping its identity."""
import httpx
import pytest

from apt_upstream import UpstreamRepo
from debs import make_deb
from mmw.core.config import MirrorConfig
from mmw.providers.apt import AptFlatProvider, AptProvider

ORIGIN = "http://origin.invalid/ubuntu"
ARCHIVE = "http://archive.invalid/archive/ubuntu/20240101T000000Z"


@pytest.fixture
def repo(tmp_path):
    up = UpstreamRepo(tmp_path, {("jammy", "main"): [make_deb(tmp_path / "u", "hello", "2.10-2")]})
    up.server.shutdown()
    return up.root / "ubuntu"


def _client(root, seen):
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if not url.startswith(ARCHIVE + "/"):
            return httpx.Response(404)
        path = root / url[len(ARCHIVE) + 1:]
        if not path.is_file():
            return httpx.Response(404)
        return httpx.Response(200, content=path.read_bytes() if request.method == "GET" else b"")
    return httpx.Client(transport=httpx.MockTransport(handle))


def _origin_client(root, seen):
    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        path = root / url[len(ORIGIN) + 1:]
        if not url.startswith(ORIGIN + "/") or not path.is_file():
            return httpx.Response(404)
        return httpx.Response(200, content=path.read_bytes() if request.method == "GET" else b"")
    return httpx.Client(transport=httpx.MockTransport(handle))


def _unit(**extra):
    return MirrorConfig(name="u", provider="apt", source_url=ORIGIN,
                        dims={"suite": "jammy", "component": "main", "arch": "amd64"}, **extra)


def test_fetch_url_defaults_to_source_url():
    assert _unit().fetch_url() == ORIGIN
    assert _unit(fetch_base=ARCHIVE).fetch_url() == ARCHIVE


def test_every_request_goes_to_the_fetch_base(repo):
    seen: list[str] = []
    unit = _unit().model_copy(update={"fetch_base": ARCHIVE})
    packages, files = AptProvider().fetch_all(unit, _client(repo, seen))
    assert packages and files
    assert seen and all(u.startswith(ARCHIVE + "/") for u in seen)
    assert all(p.url.startswith(ARCHIVE + "/") for p in packages)
    assert all(f.url.startswith(ARCHIVE + "/") for f in files)
    assert all(not (f.fallback_url or "").startswith(ORIGIN) for f in files)


def test_repo_paths_match_a_plain_fetch(repo):
    plain_seen: list[str] = []
    plain_pkgs, plain_files = AptProvider().fetch_all(_unit(), _origin_client(repo, plain_seen))
    seen: list[str] = []
    pkgs, files = AptProvider().fetch_all(_unit(fetch_base=ARCHIVE), _client(repo, seen))
    assert sorted(p.repo_path for p in pkgs) == sorted(p.repo_path for p in plain_pkgs)
    assert sorted(f.repo_path for f in files) == sorted(f.repo_path for f in plain_files)
    assert _unit(fetch_base=ARCHIVE).mirror_base == _unit().mirror_base


def test_flat_provider_uses_the_fetch_base(repo):
    seen: list[str] = []
    flat_archive = f"{ARCHIVE}/dists/jammy/main/binary-amd64"
    unit = MirrorConfig(name="f", provider="apt-flat", source_url=f"{ORIGIN}/dists/jammy/main/binary-amd64",
                        fetch_base=flat_archive)
    AptFlatProvider().fetch_packages(unit, _client(repo, seen))
    assert seen and all(u.startswith(flat_archive + "/") for u in seen)


def test_fetch_base_is_never_recorded():
    unit = _unit(fetch_base=ARCHIVE)
    assert "fetch_base" not in unit.model_dump()
    assert ARCHIVE not in unit.model_dump_json()
    assert ARCHIVE not in str(unit.legacy_dict())
    assert unit == _unit()
