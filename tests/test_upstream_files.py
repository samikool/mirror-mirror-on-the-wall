"""Files a vendor's install instructions fetch, mirrored at their own path.

`gpg_key_url` and every `extra_files` URL are kept byte-for-byte and published
at `{host}/{path}`, so once DNS points at the mirror the vendor's commands
work unchanged. Two real layouts are exercised end to end:

- NVIDIA Container Toolkit — `nvidia.github.io/libnvidia-container/gpgkey` and
  `…/stable/deb/nvidia-container-toolkit.list`, both *outside* the mirrored
  `…/stable/deb/amd64` directory, on the repository's own host.
- VS Code — `packages.microsoft.com/keys/microsoft.asc`, a sibling of the
  mirrored `packages.microsoft.com/repos/code`.

Plus `keys/` served at `/mmw-keys/` on every host.
"""
import contextlib
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from mmw.core import config, keys, upstream
from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool
from mmw.core.publisher import publish_network
from mmw.core.publisher.bundle import bundle_network
from mmw.core.publisher.nginx import nginx_files

ARMOUR = ("-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nmQINBGXf\n"
          "-----END PGP PUBLIC KEY BLOCK-----\n")
BINARY = b"\x99\x01\x0dbinary-keyring-bytes\xff"

NV_REPO = "https://nvidia.github.io/libnvidia-container/stable/deb/amd64"
NV_KEY = "https://nvidia.github.io/libnvidia-container/gpgkey"
NV_LIST = "https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list"
NV_LIST_BODY = (b"deb https://nvidia.github.io/libnvidia-container/stable/deb/$(ARCH) /\n"
                b"#deb https://nvidia.github.io/libnvidia-container/experimental/deb/$(ARCH) /\n")
MS_REPO = "https://packages.microsoft.com/repos/code"
MS_KEY = "https://packages.microsoft.com/keys/microsoft.asc"


class _Resp:
    def iter_bytes(self):
        yield self.content

    def __init__(self, content, status=200):
        self.content = content
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class _Client:
    """url -> bytes (or an int status to fail with)."""

    def __init__(self, files):
        self.files = files
        self.urls = []

    @contextlib.contextmanager
    def stream(self, method, url, **kwargs):
        yield self.get(url, **kwargs)

    def get(self, url, **kwargs):
        self.urls.append(url)
        body = self.files.get(url, 404)
        return _Resp(b"", body) if isinstance(body, int) else _Resp(body)


# --- where a URL lands ---------------------------------------------------------

@pytest.mark.parametrize("url,rel", [
    ("https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/3bf863cc.pub",
     "developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/3bf863cc.pub"),
    (NV_KEY, "nvidia.github.io/libnvidia-container/gpgkey"),
    (MS_KEY, "packages.microsoft.com/keys/microsoft.asc"),
    # Same host-with-port mangling as MirrorConfig.mirror_base, so a file on
    # its repository's host:port lands in that repository's directory.
    ("http://repo.example.com:8080/keys/repo.asc", "repo.example.com_8080/keys/repo.asc"),
])
def test_upstream_rel(url, rel):
    assert upstream.upstream_rel(url) == rel


@pytest.mark.parametrize("url", [
    "https://example.com/",                  # no file to put anywhere
    "https://example.com/key?token=abc",     # a query string has no 1-to-1 path
    "https://example.com/a/../../etc/passwd",
    "file:///etc/apt/keyrings/x.gpg",
])
def test_upstream_rel_refuses_what_has_no_safe_path(url):
    assert upstream.upstream_rel(url) is None


def test_config_refuses_an_unmirrorable_url_at_load():
    with pytest.raises(ValueError, match="cannot be mirrored at its own path"):
        config.SourceConfig(url=NV_REPO, provider="apt-flat",
                            extra_files=["https://nvidia.github.io/x?y=1"])
    with pytest.raises(ValueError, match="cannot be mirrored at its own path"):
        config.SourceConfig(url=NV_REPO, provider="apt-flat",
                            gpg_key_url="https://nvidia.github.io/")


def test_extra_files_survive_save_load_and_merge(tmp_path):
    src = config.SourceConfig(url=NV_REPO, provider="apt-flat", extra_files=[NV_LIST])
    cfg = config.Config(settings=config.Settings(mirror_dir=tmp_path / "m"), sources=[src])
    config.save(cfg, tmp_path)
    loaded = config.load(tmp_path)
    assert loaded.sources[0].extra_files == [NV_LIST]
    assert loaded.mirrors[0].extra_files == [NV_LIST]
    other = config.SourceConfig(url=NV_REPO, provider="apt-flat",
                                extra_files=[NV_LIST, "https://nvidia.github.io/x.txt"])
    merged = config.merge_sources(loaded.sources, [other])
    sources = merged[0] if isinstance(merged, tuple) else merged
    assert sources[0].extra_files == [NV_LIST, "https://nvidia.github.io/x.txt"]


# --- sync: fetch and keep ----------------------------------------------------------

def _mirror(**kw):
    fields = dict(name="nv", provider="apt-flat", label="github",
                  source=config.normalize_url(NV_REPO), source_url=NV_REPO)
    fields.update(kw)
    return config.MirrorConfig(**fields)


def _coord(tmp_path, monkeypatch, files):
    from mmw.core import sync_engine
    client = _Client(files)
    monkeypatch.setattr(sync_engine, "_thread_client", lambda: client)
    return sync_engine.KeySyncCoordinator(tmp_path), client


def _stored(tmp_path, mirror, url):
    return upstream.group_dir(tmp_path, upstream.repository_group(mirror)) / upstream.upstream_rel(url)


def test_key_url_and_extra_files_are_kept_byte_for_byte(tmp_path, monkeypatch):
    m = _mirror(gpg_key_url=NV_KEY, extra_files=[NV_LIST])
    coord, client = _coord(tmp_path, monkeypatch, {NV_KEY: BINARY, NV_LIST: NV_LIST_BODY})
    assert coord.sync_for(m) == []
    assert _stored(tmp_path, m, NV_KEY).read_bytes() == BINARY       # not re-armoured
    assert _stored(tmp_path, m, NV_LIST).read_bytes() == NV_LIST_BODY
    # The key URL was fetched once and its bytes reused for the upstream copy.
    assert client.urls.count(NV_KEY) == 1
    # keys/ still gets the normalized pair, as before.
    assert {p.suffix for p in (tmp_path / "keys" / "github").iterdir()} >= {".asc", ".gpg"}


def test_once_per_repository_across_its_mirrors(tmp_path, monkeypatch):
    coord, client = _coord(tmp_path, monkeypatch, {NV_LIST: NV_LIST_BODY})
    for arch in ("amd64", "arm64"):
        assert coord.sync_for(_mirror(name=f"nv-{arch}", arch=arch, extra_files=[NV_LIST])) == []
    assert client.urls == [NV_LIST]


def test_inline_key_with_a_url_still_fetches_the_url(tmp_path, monkeypatch):
    m = _mirror(gpg_key=ARMOUR, gpg_key_name="nv.gpg", gpg_key_url=NV_KEY)
    coord, _ = _coord(tmp_path, monkeypatch, {NV_KEY: BINARY})
    assert coord.sync_for(m) == []
    assert (tmp_path / "keys" / "github" / "nv.asc").read_text() == ARMOUR
    assert _stored(tmp_path, m, NV_KEY).read_bytes() == BINARY


def test_a_failed_fetch_is_reported_and_keeps_the_last_good_copy(tmp_path, monkeypatch):
    m = _mirror(extra_files=[NV_LIST])
    coord, _ = _coord(tmp_path, monkeypatch, {NV_LIST: NV_LIST_BODY})
    coord.sync_for(m)
    coord, _ = _coord(tmp_path, monkeypatch, {NV_LIST: 503})
    failures = coord.sync_for(m)
    assert failures and NV_LIST in failures[0]
    assert _stored(tmp_path, m, NV_LIST).read_bytes() == NV_LIST_BODY


def test_a_url_no_longer_named_is_removed(tmp_path, monkeypatch):
    coord, _ = _coord(tmp_path, monkeypatch, {NV_LIST: NV_LIST_BODY, NV_KEY: BINARY})
    coord.sync_for(_mirror(gpg_key_url=NV_KEY, extra_files=[NV_LIST]))
    coord, _ = _coord(tmp_path, monkeypatch, {NV_KEY: BINARY})
    m = _mirror(gpg_key_url=NV_KEY)
    assert coord.sync_for(m) == []
    assert not _stored(tmp_path, m, NV_LIST).exists()
    assert _stored(tmp_path, m, NV_KEY).exists()


def test_an_oversized_file_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "MAX_BYTES", 10)
    m = _mirror(extra_files=[NV_LIST])
    coord, _ = _coord(tmp_path, monkeypatch, {NV_LIST: NV_LIST_BODY})
    failures = coord.sync_for(m)
    assert failures and "MiB" in failures[0]
    assert not _stored(tmp_path, m, NV_LIST).exists()


# --- publish / bundle -------------------------------------------------------------

def _seed_repo(pool, name, url, label):
    refs = []
    for repo_path, content, kind in [
        (f"{name}_1.0_amd64.deb", f"{name}-pkg".encode(), KIND_PKG),
        ("Packages", f"{name}-index".encode(), KIND_META),
        ("InRelease", f"{name}-release".encode(), KIND_RELEASE),
    ]:
        cs = hashlib.sha256(content).hexdigest()
        pool.add(content, cs, "sha256", Path(repo_path).name, len(content))
        refs.append((cs, repo_path, kind))
    pool.reconcile_refs(name, refs, source=config.normalize_url(url))
    m = config.MirrorConfig(name=name, source=config.normalize_url(url),
                            provider="apt-flat", source_url=url, label=label)
    pool.record_mirror(m)
    return m


def _store(mirror_dir, mirror, url, body):
    upstream.store(mirror_dir, upstream.repository_group(mirror), url, body)


@pytest.fixture
def seeded(tmp_path):
    """The NVIDIA and VS Code repositories, synced, with their vendor files."""
    mirror_dir = tmp_path / "mirror"
    pool = Pool(mirror_dir)
    nv = _seed_repo(pool, "nv", NV_REPO, "github")
    ms = _seed_repo(pool, "code", MS_REPO, "microsoft")
    pool.close()
    _store(mirror_dir, nv, NV_KEY, BINARY)
    _store(mirror_dir, nv, NV_LIST, NV_LIST_BODY)
    _store(mirror_dir, ms, MS_KEY, ARMOUR.encode())
    (mirror_dir / "keys" / "github").mkdir(parents=True)
    (mirror_dir / "keys" / "github" / "nv.gpg").write_bytes(b"binary")
    cfg = config.Config(settings=config.Settings(mirror_dir=mirror_dir), sources=[
        config.SourceConfig(url=NV_REPO, provider="apt-flat"),
        config.SourceConfig(url=MS_REPO, provider="apt-flat")])
    return mirror_dir, cfg


def _rel(url):
    return upstream.upstream_rel(url)


def test_publish_places_every_vendor_file_at_its_own_path(seeded, tmp_path):
    mirror_dir, cfg = seeded
    root = tmp_path / "pub"
    result = publish_network(config.NetworkConfig(name="lab", root=root), cfg, mirror_dir)
    assert result.errors == []
    assert (root / _rel(NV_KEY)).read_bytes() == BINARY
    assert (root / _rel(NV_LIST)).read_bytes() == NV_LIST_BODY
    assert (root / _rel(MS_KEY)).read_bytes() == ARMOUR.encode()
    # The keys folder is unchanged, and no copy leaks into it.
    assert (root / "keys" / "github" / "nv.gpg").exists()
    assert not (root / "upstream").exists()


def test_vendor_files_survive_a_pruning_republish(seeded, tmp_path):
    """Microsoft's `keys/` sits beside `repos/code` and NVIDIA's files beside
    `stable/deb/amd64` — the stale-directory sweep read both as dropped
    repositories and removed them right after publishing."""
    mirror_dir, cfg = seeded
    root = tmp_path / "pub"
    net = config.NetworkConfig(name="lab", root=root, prune=True)
    publish_network(net, cfg, mirror_dir)
    publish_network(net, cfg, mirror_dir)
    for url in (NV_KEY, NV_LIST, MS_KEY):
        assert (root / _rel(url)).exists(), url


def test_a_dropped_repository_beside_vendor_files_is_still_swept(seeded, tmp_path):
    mirror_dir, cfg = seeded
    root = tmp_path / "pub"
    stale = root / "nvidia.github.io/libnvidia-container/stable/deb/arm64"
    stale.mkdir(parents=True)
    (stale / "Packages").write_text("old")
    stale_ms = root / "packages.microsoft.com/repos/edge"
    stale_ms.mkdir(parents=True)
    (stale_ms / "Packages").write_text("old")
    publish_network(config.NetworkConfig(name="lab", root=root, prune=True), cfg, mirror_dir)
    assert not stale.exists() and not stale_ms.exists()
    assert (root / _rel(NV_LIST)).exists() and (root / _rel(MS_KEY)).exists()


def test_a_file_on_a_host_with_no_repository_is_served_and_kept(seeded, tmp_path):
    mirror_dir, cfg = seeded
    nv = config.MirrorConfig(name="nv", source=config.normalize_url(NV_REPO),
                             provider="apt-flat", source_url=NV_REPO, label="github")
    _store(mirror_dir, nv, "https://keys.example.com/vendor/repo.asc", b"lone")
    root = tmp_path / "pub"
    net = config.NetworkConfig(name="lab", root=root, prune=True)
    publish_network(net, cfg, mirror_dir)
    publish_network(net, cfg, mirror_dir)
    assert (root / "keys.example.com/vendor/repo.asc").read_bytes() == b"lone"
    assert "server_name keys.example.com;" in (root / "nginx" / "mirror.conf").read_text()


def test_a_network_gets_only_its_own_repositories_files(seeded, tmp_path):
    mirror_dir, cfg = seeded
    root = tmp_path / "pub"
    net = config.NetworkConfig(name="lab", root=root,
                               select=[config.NetworkSelect(source=MS_REPO)])
    publish_network(net, cfg, mirror_dir)
    assert (root / _rel(MS_KEY)).exists()
    assert not (root / _rel(NV_LIST)).exists()


def test_bundle_delta_never_deletes_a_vendor_file(seeded, tmp_path):
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out", prune=True)
    bundle_network(net, cfg, mirror_dir)
    assert bundle_network(net, cfg, mirror_dir).deleted == 0


def test_bundle_round_trip_carries_vendor_files(seeded, tmp_path):
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out")
    result = bundle_network(net, cfg, mirror_dir)
    dest = tmp_path / "far"
    subprocess.run([sys.executable, str(tmp_path / "out" / "apply.py"),
                    str(result.out_path), "--root", str(dest), "--verify"], check=True)
    assert (dest / _rel(NV_LIST)).read_bytes() == NV_LIST_BODY
    assert (dest / _rel(MS_KEY)).read_bytes() == ARMOUR.encode()


def test_cert_names_cover_vendor_file_hosts(seeded, monkeypatch):
    from mmw.core import certs
    mirror_dir, cfg = seeded
    nv = config.MirrorConfig(name="nv", source=config.normalize_url(NV_REPO),
                             provider="apt-flat", source_url=NV_REPO, label="github")
    _store(mirror_dir, nv, "https://keys.example.com/vendor/repo.asc", b"lone")
    monkeypatch.setattr(certs, "network_mirrors", lambda n, c, d: [nv])
    names = certs.collect_hostnames(config.NetworkConfig(name="lab", certs=True), cfg)
    assert {"nvidia.github.io", "keys.example.com"} <= set(names)


# --- the keys folder over HTTP -----------------------------------------------------

def test_every_server_block_serves_the_keys_folder():
    m = config.MirrorConfig(source_url="https://ports.ubuntu.com/", name="a",
                            provider="apt", label="x")
    conf = nginx_files([m], "/srv/lab")["nginx/mirror.conf"].decode()
    assert "location /mmw-keys/ {" in conf
    assert "alias /srv/lab/keys/;" in conf


def test_keys_module_no_longer_owns_upstream_copies():
    """One implementation: upstream copies live in core/upstream.py only."""
    assert not hasattr(keys, "store_upstream_key")
    assert not hasattr(keys, "upstream_key_rel")


def test_per_arch_repositories_keep_their_own_upstream_files(tmp_path, monkeypatch):
    """NVIDIA's amd64 and arm64 flat repositories derived one directory name,
    so the second never fetched its own files and each pruned the other's."""
    amd = "https://nvidia.github.io/libnvidia-container/stable/deb/amd64"
    arm = "https://nvidia.github.io/libnvidia-container/stable/deb/arm64"
    a = _mirror(name="a", source=config.normalize_url(amd), source_url=amd,
                extra_files=["https://x.test/amd.list"])
    b = _mirror(name="b", source=config.normalize_url(arm), source_url=arm,
                extra_files=["https://x.test/arm.list"])
    assert upstream.repository_group(a) != upstream.repository_group(b)
    coord, _ = _coord(tmp_path, monkeypatch, {"https://x.test/amd.list": b"amd",
                                              "https://x.test/arm.list": b"arm"})
    assert coord.sync_for(a) == [] and coord.sync_for(b) == []
    assert _stored(tmp_path, a, "https://x.test/amd.list").read_bytes() == b"amd"
    assert _stored(tmp_path, b, "https://x.test/arm.list").read_bytes() == b"arm"


def test_files_filed_under_the_old_directory_are_still_published(tmp_path):
    from mmw.core.publisher.links import upstream_files
    m = _mirror()
    old = upstream.group_dir(tmp_path, upstream.legacy_group(m)) / upstream.upstream_rel(NV_KEY)
    old.parent.mkdir(parents=True)
    old.write_bytes(BINARY)
    assert upstream_files(tmp_path, [m]) == {upstream.upstream_rel(NV_KEY): old}


def test_the_size_cap_stops_an_endless_stream(monkeypatch):
    monkeypatch.setattr(upstream, "MAX_BYTES", 1000)

    class _Endless:
        def raise_for_status(self):
            pass

        def iter_bytes(self):
            while True:
                yield b"x" * 100

    class _Client:
        @contextlib.contextmanager
        def stream(self, *a, **k):
            yield _Endless()

    with pytest.raises(ValueError, match="MiB"):
        upstream.fetch_bytes("https://x.test/f", _Client())
