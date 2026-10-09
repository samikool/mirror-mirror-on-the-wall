"""Repository keys in two places: `keys/{label}/` (both encodings, served at
`/mmw-keys/`), and — when the key came from a URL — byte-for-byte at that
URL's own path, so a vendor's install instructions work unchanged against
the mirror."""
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from mmw.core import config, keys
from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool
from mmw.core.publisher import publish_network
from mmw.core.publisher.bundle import bundle_network
from mmw.core.publisher.nginx import nginx_files

ARMOUR = ("-----BEGIN PGP PUBLIC KEY BLOCK-----\n\nmQINBGXf\n"
          "-----END PGP PUBLIC KEY BLOCK-----\n")
BINARY = b"\x99\x01\x0dbinary-keyring-bytes\xff"


class _Resp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


class _Client:
    def __init__(self, content):
        self.content = content
        self.urls = []

    def get(self, url, **kwargs):
        self.urls.append(url)
        return _Resp(self.content)


# --- where a key URL lands ---------------------------------------------------

@pytest.mark.parametrize("url,rel", [
    ("https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/3bf863cc.pub",
     "developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/3bf863cc.pub"),
    ("https://nvidia.github.io/libnvidia-container/gpgkey",
     "nvidia.github.io/libnvidia-container/gpgkey"),
    # Same host-with-port mangling as MirrorConfig.mirror_base, so a key on
    # the same host:port as its repository lands in the same directory.
    ("http://repo.example.com:8080/keys/repo.asc", "repo.example.com_8080/keys/repo.asc"),
])
def test_upstream_key_rel(url, rel):
    assert keys.upstream_key_rel(url) == rel


@pytest.mark.parametrize("url", [
    "https://example.com/",                  # no file to put anywhere
    "https://example.com/key?token=abc",     # a query string has no 1-to-1 path
    "https://example.com/a/../../etc/passwd",
    "file:///etc/apt/keyrings/x.gpg",
])
def test_upstream_key_rel_refuses_what_has_no_safe_path(url):
    assert keys.upstream_key_rel(url) is None


def test_fetched_key_is_kept_byte_for_byte_at_its_url_path(tmp_path):
    url = "https://nvidia.github.io/libnvidia-container/gpgkey"
    r = keys.sync_custom_key("github", url, tmp_path, _Client(BINARY))
    assert r.ok
    kept = tmp_path / "keys" / "github" / "upstream" / "nvidia.github.io/libnvidia-container/gpgkey"
    assert kept.read_bytes() == BINARY          # not re-armoured, not renamed
    # ...and the normalized pair is still written beside it, as before.
    assert {p.suffix for p in (tmp_path / "keys" / "github").iterdir()
            if p.is_file()} >= {".asc", ".gpg"}


def test_inline_key_with_a_url_writes_both(tmp_path, monkeypatch):
    """A deb822 import gives an inline key; the vendor also publishes it at a
    URL. The inline key fills keys/, the URL is mirrored 1-to-1."""
    from mmw.core import sync_engine
    client = _Client(BINARY)
    monkeypatch.setattr(sync_engine, "_thread_client", lambda: client)
    m = config.MirrorConfig(
        name="cuda", provider="apt-flat", label="nvidia",
        source_url="https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/",
        gpg_key=ARMOUR, gpg_key_name="cuda_debian_prod.gpg",
        gpg_key_url="https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/3bf863cc.pub",
    )
    coord = sync_engine.KeySyncCoordinator(tmp_path)
    assert coord.sync_for(m) == []
    d = tmp_path / "keys" / "nvidia"
    assert (d / "cuda_debian_prod.asc").read_text() == ARMOUR
    assert (d / "upstream" / keys.upstream_key_rel(m.gpg_key_url)).read_bytes() == BINARY
    # Claimed once: a second mirror of the same source fetches nothing more.
    coord.sync_for(m)
    assert len(client.urls) == 1


# --- publish / bundle ---------------------------------------------------------

SRC = "https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64"
IN_BASE = "https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/3bf863cc.pub"
OFF_BASE = "https://nvidia.github.io/libnvidia-container/gpgkey"


@pytest.fixture
def seeded(tmp_path):
    """One flat mirror, plus two upstream keys already fetched: one inside the
    repository's own directory, one on another host entirely."""
    mirror_dir = tmp_path / "mirror"
    pool = Pool(mirror_dir)
    refs = []
    for repo_path, content, kind in [
        ("cuda_1.0_amd64.deb", b"cuda-pkg", KIND_PKG),
        ("Packages", b"pkg-index", KIND_META),
        ("InRelease", b"signed-release", KIND_RELEASE),
    ]:
        cs = hashlib.sha256(content).hexdigest()
        pool.add(content, cs, "sha256", Path(repo_path).name, len(content))
        refs.append((cs, repo_path, kind))
    pool.reconcile_refs("cuda", refs, source=SRC)
    pool.record_mirror(config.MirrorConfig(
        name="cuda", source=SRC, provider="apt-flat", source_url=SRC, label="nvidia"))
    pool.close()
    for url, body in ((IN_BASE, b"in-base-key"), (OFF_BASE, b"off-base-key")):
        p = mirror_dir / "keys" / "nvidia" / "upstream" / keys.upstream_key_rel(url)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
    (mirror_dir / "keys" / "nvidia" / "cuda_debian_prod.gpg").write_bytes(b"binary")
    cfg = config.Config(settings=config.Settings(mirror_dir=mirror_dir),
                        sources=[config.SourceConfig(url=SRC, provider="apt-flat")])
    return mirror_dir, cfg


def test_publish_places_keys_at_their_upstream_paths(seeded, tmp_path):
    mirror_dir, cfg = seeded
    root = tmp_path / "pub"
    result = publish_network(config.NetworkConfig(name="lab", root=root), cfg, mirror_dir)
    assert result.errors == []
    assert (root / keys.upstream_key_rel(IN_BASE)).read_bytes() == b"in-base-key"
    assert (root / keys.upstream_key_rel(OFF_BASE)).read_bytes() == b"off-base-key"
    # The keys folder is unchanged, and the raw copies are not duplicated into it.
    assert (root / "keys" / "nvidia" / "cuda_debian_prod.gpg").exists()
    assert not (root / "keys" / "nvidia" / "upstream").exists()
    conf = (root / "nginx" / "mirror.conf").read_text()
    assert "server_name nvidia.github.io;" in conf        # the key's own host is served


def test_a_key_inside_the_repo_directory_survives_a_pruning_republish(seeded, tmp_path):
    """It is not in the repository's index, so the group's prune would treat
    it as stale — and on a bundle, schedule it for deletion on the far side."""
    mirror_dir, cfg = seeded
    root = tmp_path / "pub"
    net = config.NetworkConfig(name="lab", root=root, prune=True)
    publish_network(net, cfg, mirror_dir)
    publish_network(net, cfg, mirror_dir)
    assert (root / keys.upstream_key_rel(IN_BASE)).exists()


def test_bundle_delta_never_deletes_an_upstream_key(seeded, tmp_path):
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out", prune=True)
    bundle_network(net, cfg, mirror_dir)
    second = bundle_network(net, cfg, mirror_dir)
    # Nothing changed, so nothing may be deleted — the in-base key used to
    # look "not in the index" to that group's prune.
    assert second.deleted == 0


def test_bundle_round_trip_carries_upstream_keys(seeded, tmp_path):
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out")
    result = bundle_network(net, cfg, mirror_dir)
    dest = tmp_path / "far"
    subprocess.run([sys.executable, str(tmp_path / "out" / "apply.py"),
                    str(result.out_path), "--root", str(dest), "--verify"], check=True)
    assert (dest / keys.upstream_key_rel(OFF_BASE)).read_bytes() == b"off-base-key"


# --- the keys folder over HTTP -----------------------------------------------

def test_every_server_block_serves_the_keys_folder():
    m = config.MirrorConfig(source_url="https://ports.ubuntu.com/", name="a",
                            provider="apt", label="x")
    conf = nginx_files([m], "/srv/lab")["nginx/mirror.conf"].decode()
    assert "location /mmw-keys/ {" in conf
    assert "alias /srv/lab/keys/;" in conf


def test_a_key_on_a_host_with_no_repository_survives_the_stale_sweep(seeded, tmp_path):
    """`prune_stale_bases` runs after keys are linked, and removes every
    top-level directory no repository produced — unless told otherwise."""
    mirror_dir, cfg = seeded
    url = "https://keys.example.com/vendor/repo.asc"
    p = mirror_dir / "keys" / "nvidia" / "upstream" / keys.upstream_key_rel(url)
    p.parent.mkdir(parents=True)
    p.write_bytes(b"lone-key")
    root = tmp_path / "pub"
    net = config.NetworkConfig(name="lab", root=root, prune=True)
    publish_network(net, cfg, mirror_dir)
    publish_network(net, cfg, mirror_dir)
    assert (root / "keys.example.com/vendor/repo.asc").read_bytes() == b"lone-key"


def test_a_dropped_repository_beside_a_key_is_still_swept(seeded, tmp_path):
    """The live-directory exemption is only for hosts with no repository —
    it must not shield a stale repository on a host that has one."""
    mirror_dir, cfg = seeded
    root = tmp_path / "pub"
    stale = root / "developer.download.nvidia.com/compute/cuda/repos/old-distro/x86_64"
    stale.mkdir(parents=True)
    (stale / "Packages").write_text("old")
    publish_network(config.NetworkConfig(name="lab", root=root, prune=True), cfg, mirror_dir)
    assert not stale.exists()
    assert (root / keys.upstream_key_rel(IN_BASE)).exists()


def test_cert_names_cover_upstream_key_hosts(seeded, monkeypatch):
    from mmw.core import certs
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", certs=True)
    monkeypatch.setattr(certs, "network_mirrors", lambda n, c, d: [config.MirrorConfig(
        name="cuda", source=SRC, provider="apt-flat", source_url=SRC, label="nvidia")])
    assert "nvidia.github.io" in certs.collect_hostnames(net, cfg)
