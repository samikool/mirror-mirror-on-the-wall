"""Stage-1 safety net for the provider seam.

A fixed network — a two-suite apt repository with dist-upgrade on, an apt-flat
repository, a key in keys/, an upstream file at its own path — must publish and
bundle exactly the same bytes before and after the refactor.

Generated once, against the pre-refactor code:
    MMW_UPDATE_GOLDEN=1 uv run pytest tests/test_seam_golden.py
Never regenerate it to make a later change pass — a diff here IS the bug.
"""
import hashlib
import json
import os
import tarfile
from pathlib import Path

from mmw.core import config, upstream
from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool
from mmw.core.publisher import bundle_network, publish_network

GOLDEN = Path(__file__).parent / "golden" / "seam_publish.json"
APT = "https://archive.ubuntu.com/ubuntu"
FLAT = "https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64"
FLAT_KEY = FLAT + "/3bf863cc.pub"
META_RELEASE_LTS = (
    b"Dist: jammy\nName: Jammy Jellyfish\nVersion: 22.04.4 LTS\n\n"
    b"Dist: noble\nName: Noble Numbat\nVersion: 24.04 LTS\n\n"
)


def _add(pool: Pool, content: bytes, name: str) -> str:
    cs = hashlib.sha256(content).hexdigest()
    pool.add(content, cs, "sha256", name, len(content))
    return cs


def _seed(pool: Pool, mirror: config.MirrorConfig, rows) -> None:
    refs = [(_add(pool, content, Path(path).name), path, kind) for path, content, kind in rows]
    pool.reconcile_refs(mirror.name, refs, source=mirror.source)
    pool.record_mirror(mirror)


def _build(tmp_path: Path) -> tuple[Path, config.Config]:
    mirror_dir = tmp_path / "mirror"
    apt_src = config.SourceConfig(url=APT, suites=["jammy", "noble"], components=["main"],
                                  arches=["amd64"], dist_upgrade=True)
    flat_src = config.SourceConfig(url=FLAT, provider="apt-flat")
    pool = Pool(mirror_dir)
    for m in apt_src.to_mirrors():
        rows = [
            ("pool/main/h/hello/hello_1.0_amd64.deb", b"hello-pkg", KIND_PKG),
            (f"dists/{m.suite}/main/binary-amd64/Packages.gz", f"pkgs-{m.suite}".encode(), KIND_META),
            (f"dists/{m.suite}/InRelease", f"rel-{m.suite}".encode(), KIND_RELEASE),
        ]
        if m.suite == "jammy":
            rows += [
                ("changelogs.ubuntu.com/meta-release", b"Dist: jammy\n\n", KIND_META),
                ("changelogs.ubuntu.com/meta-release-lts", META_RELEASE_LTS, KIND_META),
            ]
        _seed(pool, m, rows)
    (flat,) = flat_src.to_mirrors()
    _seed(pool, flat, [
        ("cuda-12_12.0_amd64.deb", b"cuda-pkg", KIND_PKG),
        ("Packages.gz", b"flat-pkgs", KIND_META),
        ("InRelease", b"flat-rel", KIND_RELEASE),
    ])
    pool.close()
    keys = mirror_dir / "keys" / "ubuntu"
    keys.mkdir(parents=True)
    (keys / "ubuntu-archive-keyring.asc").write_bytes(b"-----BEGIN PGP PUBLIC KEY BLOCK-----\nx\n")
    upstream.store(mirror_dir, upstream.repository_group(flat), FLAT_KEY, b"flat-gpg-key")
    cfg = config.Config(settings=config.Settings(mirror_dir=mirror_dir),
                        sources=[apt_src, flat_src])
    return mirror_dir, cfg


def _digest(data: bytes, root: Path) -> str:
    return hashlib.sha256(data.replace(str(root).encode(), b"<ROOT>")).hexdigest()


def _capture(tmp_path: Path) -> dict:
    mirror_dir, cfg = _build(tmp_path)
    root = tmp_path / "published"
    publish_network(config.NetworkConfig(name="lab", root=root, dist_upgrade=True, certs=False),
                    cfg, mirror_dir)
    published = {
        p.relative_to(root).as_posix(): _digest(p.read_bytes(), root)
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.relative_to(root).as_posix().startswith(".mmw/")
    }
    result = bundle_network(
        config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out", dist_upgrade=True,
                             certs=False),
        cfg, mirror_dir)
    bundled = {}
    with tarfile.open(result.out_path) as tar:
        for member in tar:
            if (member.isfile() or member.islnk()) and not member.name.startswith(".mmw/"):
                bundled[member.name] = hashlib.sha256(tar.extractfile(member).read()).hexdigest()
    return {"published": published, "bundled": bundled}


def test_seam_publish_and_bundle_are_unchanged(tmp_path):
    actual = _capture(tmp_path)
    # The fixture must keep exercising every path the refactor moves.
    pub = actual["published"]
    assert "UPGRADE.md" in pub
    assert "changelogs.ubuntu.com/meta-release-lts" in pub
    assert "keys/ubuntu/ubuntu-archive-keyring.asc" in pub
    assert "developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/3bf863cc.pub" in pub
    assert any("/by-hash/SHA256/" in rel for rel in pub)
    assert "nginx/mirror.conf" in pub
    if os.environ.get("MMW_UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(actual, indent=2, sort_keys=True) + "\n")
    expected = json.loads(GOLDEN.read_text())
    # The original capture omitted hardlinks (the by-hash aliases). Compare
    # the whole logical tree: their original bytes are already recorded in
    # published. Bundle-specific bytes, such as nginx's root placeholder,
    # still come from bundled. Which alias carries the tar payload is not
    # significant, and changed when release files began shipping last.
    expected["bundled"] = expected["published"] | expected["bundled"]
    assert actual == expected
