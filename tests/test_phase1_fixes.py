"""Regression tests for the Phase 1 bug fixes."""
import hashlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from mmw.core import config

runner = CliRunner()


# ── cli.py: headless publish must actually run ───────────────────────────────

def _write_publishable_setup(root: Path) -> Path:
    """Config + synced pool (refs + blobs) + network, all under root. Returns dst base."""
    from mmw.core.pool import KIND_PKG, KIND_RELEASE, Pool

    mirror_dir = root / "mirror"
    cfg = config.Config(
        settings=config.Settings(mirror_dir=mirror_dir, max_connections=2),
        sources=[config.SourceConfig(
            name="custom-repo", label="custom", provider="apt-flat",
            url="https://example.com/repo/",
        )],
        networks=[config.NetworkConfig(name="lab", root=root / "pub")],
    )
    config.save(cfg, root)

    with Pool(mirror_dir) as pool:
        pkg = hashlib.sha256(b"deb-bytes").hexdigest()
        pool.add(b"deb-bytes", pkg, "sha256", "bash.deb", 9)
        rel = hashlib.sha256(b"<repomd/>").hexdigest()
        pool.add(b"<repomd/>", rel, "sha256", "repomd.xml", 9)
        pool.reconcile_refs("custom-repo", [
            (pkg, "Packages/bash.deb", KIND_PKG),
            (rel, "repodata/repomd.xml", KIND_RELEASE),
        ])
    return root / "pub" / "example.com" / "repo"


def test_publish_quiet_links_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dst = _write_publishable_setup(tmp_path)
    from mmw.cli import app
    result = runner.invoke(app, ["publish", "--quiet"])
    assert result.exit_code == 0, result.output
    assert (dst / "Packages" / "bash.deb").exists()
    assert (dst / "repodata" / "repomd.xml").exists()


def test_publish_dry_run_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    dst = _write_publishable_setup(tmp_path)
    from mmw.cli import app
    result = runner.invoke(app, ["publish", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert not (dst / "Packages").exists()


def test_publish_unknown_network_errors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _write_publishable_setup(tmp_path)
    from mmw.cli import app
    result = runner.invoke(app, ["publish", "--quiet", "--network", "nope"])
    assert result.exit_code == 1


# ── config.py: unknown keys must fail loudly ─────────────────────────────────

def test_unknown_config_keys_rejected(tmp_path):
    config.config_path(tmp_path).write_text(
        '[settings]\nmirror_dir = "/x"\n\n[[mirrors]]\nname = "old-style"\n'
    )
    with pytest.raises(Exception):
        config.load(tmp_path)


def test_unknown_source_keys_rejected(tmp_path):
    config.config_path(tmp_path).write_text(
        '[settings]\nmirror_dir = "/x"\n\n'
        '[[sources]]\nname = "n"\ndistro = "d"\nprovider = "rpm"\n'
        'url = "http://x"\nlocal_path = "gone"\n'
    )
    with pytest.raises(Exception):
        config.load(tmp_path)


def test_cli_reports_invalid_config_cleanly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config.config_path(tmp_path).write_text('[settings]\nmirror_dir = "/x"\nbogus = 1\n')
    from mmw.cli import app
    result = runner.invoke(app, ["sync", "--quiet"])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)


# ── apt parsers: continuation lines must not poison fields ───────────────────

def test_continuation_line_cannot_override_sha256():
    from mmw.providers.apt import _parse_packages
    real = hashlib.sha256(b"x").hexdigest()
    text = (
        "Package: evil\n"
        "Size: 10\n"
        "Filename: pool/main/e/evil/evil.deb\n"
        f"SHA256: {real}\n"
        "Description: first line\n"
        f" SHA256: {'f' * 64}\n"
        "\n"
    )
    pkgs = _parse_packages(text, "http://x")
    assert len(pkgs) == 1
    assert pkgs[0].checksum == real


def test_sources_continuation_line_cannot_override_directory():
    from mmw.providers.apt import _parse_sources
    text = (
        "Package: evil\n"
        "Directory: pool/main/e/evil\n"
        "Notes: multi\n"
        " Directory: ../../escape\n"
        "Checksums-Sha256:\n"
        f" {'a' * 64} 100 evil.dsc\n"
        "\n"
    )
    pkgs = _parse_sources(text, "http://x")
    assert len(pkgs) == 1
    assert pkgs[0].repo_path == "pool/main/e/evil/evil.dsc"


# ── rpm provider: legacy "sha" checksum type ─────────────────────────────────

# ── certs: existing key is never regenerated ─────────────────────────────────

def test_existing_key_preserved(tmp_path):
    from mmw.core.certs import generate_key_and_csr
    key_path, _, csr1 = generate_key_and_csr(["a.example"], tmp_path)
    original_key = key_path.read_bytes()

    key_path2, _, csr2 = generate_key_and_csr(["a.example", "b.example"], tmp_path)
    assert key_path2.read_bytes() == original_key  # key untouched
    assert b"CERTIFICATE REQUEST" in csr2.read_bytes()  # CSR regenerated


# ── publisher: shared-base owner semantics ───────────────────────────────────

def test_shared_base_components_publish_as_one_group(tmp_path):
    """Two apt component mirrors share a base: published once as a group —
    shared files (the package, InRelease) linked exactly once."""
    from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool
    from mmw.core.publisher import publish_network

    mirror_dir = tmp_path / "mirror"
    base = "deb.example.com/debian"

    with Pool(mirror_dir) as pool:
        deb = hashlib.sha256(b"deb").hexdigest()
        pool.add(b"deb", deb, "sha256", "hello.deb", 3)
        rel = hashlib.sha256(b"release").hexdigest()
        pool.add(b"release", rel, "sha256", "InRelease", 7)
        for comp in ("main", "contrib"):
            idx = hashlib.sha256(f"packages-{comp}".encode()).hexdigest()
            pool.add(f"packages-{comp}".encode(), idx, "sha256", "Packages.xz", 13)
            pool.reconcile_refs(f"deb-bookworm-bookworm-{comp}-amd64", [
                (deb, "pool/main/h/hello/hello.deb", KIND_PKG),
                (idx, f"dists/bookworm/{comp}/binary-amd64/Packages.xz", KIND_META),
                (rel, "dists/bookworm/InRelease", KIND_RELEASE),
            ])

    net = config.NetworkConfig(name="n", root=tmp_path / "pub", prune=False)
    cfg = config.Config(
        settings=config.Settings(mirror_dir=mirror_dir),
        sources=[config.SourceConfig(
            name="deb-bookworm", label="debian", provider="apt",
            url="https://deb.example.com/debian", arches=["amd64"],
            suites=["bookworm"], components=["main", "contrib"],
        )],
        networks=[net],
    )

    result = publish_network(net, cfg, mirror_dir)
    assert result.errors == []
    # one group result for the shared base
    assert len(result.mirrors) == 1
    group = result.mirrors[0]
    # package + 2 component indexes + shared InRelease = 4, each linked once
    assert group.linked == 4
    dst = tmp_path / "pub" / base
    assert (dst / "pool/main/h/hello/hello.deb").exists()
    assert (dst / "dists/bookworm/contrib/binary-amd64/Packages.xz").exists()
    assert (dst / "dists/bookworm/InRelease").exists()
