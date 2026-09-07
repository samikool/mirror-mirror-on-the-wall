import hashlib
import tarfile
from pathlib import Path

import pytest
import zstandard

from mmw.core import config
from mmw.core.pool import KIND_META, KIND_PKG, KIND_RELEASE, Pool
from mmw.core.publisher import publish_network
from mmw.core.publisher.bundle import bundle_network


@pytest.fixture
def seeded(tmp_path):
    """A pool holding one two-component apt mirror, refs reconciled."""
    mirror_dir = tmp_path / "mirror"
    pool = Pool(mirror_dir)
    src = "https://archive.ubuntu.com/ubuntu"
    rows = [
        ("pool/main/h/hello/hello_1.0_amd64.deb", b"hello-pkg", KIND_PKG),
        ("dists/noble/main/binary-amd64/Packages.gz", b"pkg-index", KIND_META),
        ("dists/noble/InRelease", b"signed-release", KIND_RELEASE),
    ]
    refs = []
    for repo_path, content, kind in rows:
        cs = hashlib.sha256(content).hexdigest()
        pool.add(content, cs, "sha256", Path(repo_path).name, len(content))
        refs.append((cs, repo_path, kind))
    pool.reconcile_refs("ubuntu-noble-main-amd64", refs, source=src)
    pool.record_mirror(config.MirrorConfig(
        name="ubuntu-noble-main-amd64", source=src, provider="apt",
        source_url=src, arch="amd64", suite="noble", component="main",
        label="ubuntu",
    ))
    pool.close()
    cfg = config.Config(
        settings=config.Settings(mirror_dir=mirror_dir),
        sources=[config.SourceConfig(url=src, suites=["noble"],
                                     components=["main"], arches=["amd64"])],
    )
    return mirror_dir, cfg


def _extract(bundle: Path, dest: Path) -> None:
    dctx = zstandard.ZstdDecompressor()
    dest.mkdir(parents=True, exist_ok=True)
    with open(bundle, "rb") as raw, dctx.stream_reader(raw) as zr:
        with tarfile.open(fileobj=zr, mode="r|") as tar:
            tar.extractall(dest)


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*") if p.is_file()
    }


def test_bundle_matches_publish_exactly(seeded, tmp_path):
    """THE test. One enumeration, two sinks — the outputs must not diverge."""
    mirror_dir, cfg = seeded

    pub_root = tmp_path / "published"
    net_pub = config.NetworkConfig(name="lab", root=pub_root)
    publish_network(net_pub, cfg, mirror_dir)

    net_bun = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out")
    result = bundle_network(net_bun, cfg, mirror_dir)

    applied = tmp_path / "applied"
    _extract(result.out_path, applied)

    # nginx/ is destination-scaffolding, not repository content: its conf
    # embeds the literal serving root, which a live publish knows
    # (`net_pub.root`) and a bundle deliberately does not (ROOT_PLACEHOLDER,
    # resolved by apply.py at the far side) — see nginx.py's own docstring
    # and tests/test_nginx_conf.py::test_placeholder_root_is_left_for_apply_time.
    # Byte-identical nginx/ between the two destinations is therefore not
    # possible, nor desired; excluded here exactly like `.mmw/` is, for the
    # same reason — it is not "what the network contains".
    published = {k: v for k, v in _tree(pub_root).items() if not k.startswith("nginx/")}
    bundled = {
        k: v for k, v in _tree(applied).items()
        if not k.startswith(".mmw/") and not k.startswith("nginx/")
    }

    assert bundled == published


def test_second_bundle_of_unchanged_pool_sends_nothing(seeded, tmp_path):
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out")

    first = bundle_network(net, cfg, mirror_dir)
    assert first.sent > 0
    assert first.previous is None

    second = bundle_network(net, cfg, mirror_dir)
    assert second.sent == 0
    assert second.previous == first.created

    applied = tmp_path / "applied2"
    _extract(second.out_path, applied)
    payload = [k for k in _tree(applied) if not k.startswith(".mmw/")]
    assert payload == []


def test_changed_ref_is_carried_and_manifest_stays_complete(seeded, tmp_path):
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out")
    bundle_network(net, cfg, mirror_dir)

    # Republish the index with new content, as an upstream sync would.
    pool = Pool(mirror_dir)
    new = b"pkg-index-v2"
    cs_new = hashlib.sha256(new).hexdigest()
    pool.add(new, cs_new, "sha256", "Packages.gz", len(new))
    keep = [
        (hashlib.sha256(b"hello-pkg").hexdigest(),
         "pool/main/h/hello/hello_1.0_amd64.deb", KIND_PKG),
        (cs_new, "dists/noble/main/binary-amd64/Packages.gz", KIND_META),
        (hashlib.sha256(b"signed-release").hexdigest(),
         "dists/noble/InRelease", KIND_RELEASE),
    ]
    pool.reconcile_refs("ubuntu-noble-main-amd64", keep,
                        source="https://archive.ubuntu.com/ubuntu")
    pool.close()

    second = bundle_network(net, cfg, mirror_dir)
    # The changed index has two paths pointing at it: the canonical
    # Packages.gz and its by-hash/SHA256/{checksum} companion (links.py's
    # `_link_rows` always emits both for an apt index file). The checksum
    # changed, so the by-hash path is new too — a real path the far side does
    # not have yet, not a duplicate of the canonical one — and BundleSink
    # counts every accepted `link()` call, matching TreeSink's behaviour of
    # actually linking both (TreeSink just doesn't fold the by-hash call into
    # its caller-facing `linked` count — see links.py's `_link_rows`).
    assert second.sent == 2

    applied = tmp_path / "applied3"
    _extract(second.out_path, applied)
    payload = {k for k in _tree(applied) if not k.startswith(".mmw/")}
    assert payload == {
        "archive.ubuntu.com/ubuntu/dists/noble/main/binary-amd64/Packages.gz",
        "archive.ubuntu.com/ubuntu/dists/noble/main/binary-amd64/by-hash/SHA256/"
        + hashlib.sha256(b"pkg-index-v2").hexdigest(),
    }

    from mmw.core.publisher.manifest import read_manifest
    _, files = read_manifest(second.manifest_path)
    # The manifest lists the network's whole state (7 rows: pkg, meta
    # canonical + by-hash, release canonical + by-hash, and the two
    # unchanged-since-last-bundle nginx files) — but the tar only carries
    # what's new since the last bundle (5: the 2 changed payload files plus
    # the 3 `.mmw/` control files), since the 2 nginx files are identical to
    # what the far side already has from the first bundle and are never
    # re-sent. +2 is exactly that gap, not a coincidence.
    assert len(files) == len(_tree(applied)) + 2   # whole tree, not the delta


def test_full_ignores_the_ledger(seeded, tmp_path):
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out")
    first = bundle_network(net, cfg, mirror_dir)
    again = bundle_network(net, cfg, mirror_dir, full=True)
    assert again.sent == first.sent
    assert again.full is True


def test_certs_never_travel(seeded, tmp_path):
    """Parity with publish, which keeps mirror.key outside network.root so a
    misconfigured server can never serve it. A private key on transfer media is
    a worse exposure than the case that layout was designed to prevent."""
    mirror_dir, cfg = seeded
    certs = mirror_dir / "certs" / "lab"
    certs.mkdir(parents=True)
    (certs / "mirror.key").write_bytes(b"-----BEGIN PRIVATE KEY-----\n")

    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out", certs=True)
    result = bundle_network(net, cfg, mirror_dir)

    applied = tmp_path / "applied-certs"
    _extract(result.out_path, applied)
    assert not any("certs" in k or "mirror.key" in k for k in _tree(applied))


def test_bundle_without_destination_is_an_error(seeded, tmp_path):
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab")
    with pytest.raises(ValueError, match="no bundle destination"):
        bundle_network(net, cfg, mirror_dir)
