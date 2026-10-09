"""Phase 3: path traversal and key placement."""
import asyncio
import hashlib

import pytest

from mmw.core.downloader import download_mirror, is_safe_repo_path
from mmw.core.pool import Pool
from mmw.providers.base import MetadataFile, PackageInfo


@pytest.mark.parametrize("path", [
    "pool/main/h/hello/hello.deb",
    "Packages/b/bash.rpm",
    "repodata/abc-primary.xml.gz",
    "dists/bookworm/main/binary-amd64/Packages.xz",
    "a/./b.rpm",
])
def test_safe_paths_accepted(path):
    assert is_safe_repo_path(path)


@pytest.mark.parametrize("path", [
    "/etc/cron.d/evil",
    "../escape.rpm",
    "a/../../escape.rpm",
    "a/b/../../../../etc/passwd",
    "..",
    "",
    "a\\..\\b",
    "a/\x00b",
])
def test_traversal_paths_rejected(path):
    assert not is_safe_repo_path(path)


def test_malicious_package_path_marked_failed(tmp_path):
    """A package whose Filename escapes the tree fails — nothing written outside."""
    pool = Pool(tmp_path / "mirror")
    try:
        evil = PackageInfo(
            name="evil", url="http://x/evil.deb",
            checksum=hashlib.sha256(b"x").hexdigest(), checksum_type="sha256",
            size=1, repo_path="../../outside/evil.deb", filename="evil.deb",
        )
        summary = asyncio.run(download_mirror(
            packages=[evil], metadata=[],
            pool=pool, mirror_name="m", max_connections=2,
        ))
        assert summary.failed == 1
        assert "unsafe path" in summary.errors[0]
        assert not (tmp_path / "outside").exists()
        assert not (tmp_path / "mirror" / "outside").exists()
    finally:
        pool.close()


def test_malicious_metadata_path_blocks_commit(tmp_path):
    pool = Pool(tmp_path / "mirror")
    try:
        evil = MetadataFile(url="http://x/f", repo_path="../../evil-index", content=b"evil")
        release = MetadataFile(url="http://x/InRelease", repo_path="InRelease", content=b"rel", is_release=True)
        summary = asyncio.run(download_mirror(
            packages=[], metadata=[evil, release],
            pool=pool, mirror_name="m", max_connections=2,
        ))
        assert summary.metadata_ok is False
        assert "unsafe metadata path" in summary.metadata_errors[0]
        assert not (tmp_path / "evil-index").exists()
        # the whole metadata set is withheld, including the (safe) release file
        assert not (tmp_path / "mirror" / "example.com/repo" / "InRelease").exists()
    finally:
        pool.close()


def test_cert_dir_outside_publish_root(tmp_path):
    from mmw.core import config
    from mmw.core.certs import cert_dir, generate_certs

    cfg = config.Config(
        settings=config.Settings(mirror_dir=tmp_path / "mirror"),
        sources=[config.SourceConfig(
            name="s", label="custom", provider="apt-flat", url="https://repo.example.com/x",
        )],
        networks=[],
    )
    net = config.NetworkConfig(name="lab", root=tmp_path / "pub")

    d = cert_dir(net, cfg)
    assert not d.is_relative_to(net.root)
    assert d.is_relative_to(tmp_path / "mirror")

    hostnames, key_path, cnf_path, csr_path, md_path = generate_certs(net, cfg)
    assert hostnames == ["repo.example.com"]
    assert key_path.is_relative_to(tmp_path / "mirror")
    assert (key_path.stat().st_mode & 0o777) == 0o600
    assert not (net.root / "certs").exists()
