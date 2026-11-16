import hashlib
import tarfile
from pathlib import Path

import pytest

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
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(bundle, mode="r|") as tar:
        # filter="data" is the modern (3.12+) extraction filter: it
        # rejects absolute paths, `..` traversal, device files, and
        # strips owner/permission bits that could affect files outside
        # the archive. Test-only convenience — the far side ships to
        # far sides as old as RHEL 8's Python 3.6, where `filter=` does
        # not exist, so it must do its own path validation rather than
        # lean on this.
        tar.extractall(dest, filter="data")


def _tree(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*") if p.is_file()
    }


def test_bundle_network_forwards_archive_progress(seeded, tmp_path):
    """`bundle_network` threads its `progress` callback into the bundle sink so
    the archive-writing phase — the actual I/O, which happens entirely
    inside `close()` — is visible too, not just the fast in-memory
    enumeration phase `publish_group` reports on."""
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out")

    events: list[str] = []
    bundle_network(net, cfg, mirror_dir, progress=lambda rel, status: events.append(status))

    assert any(s.startswith("archive-start:") for s in events)
    assert any(s.startswith("archive-done:") for s in events)


def test_full_ignores_the_ledger(seeded, tmp_path):
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out")
    first = bundle_network(net, cfg, mirror_dir)
    again = bundle_network(net, cfg, mirror_dir, from_ref=None)
    assert again.sent == first.sent
    assert again.full is True


def test_the_certs_directory_never_travels(seeded, tmp_path):
    """Parity with publish, which keeps mirror.key outside network.root so a
    misconfigured server can never serve it. A certs network's bundle carries
    its TLS files — but only into `.mmw/tls/`, never a served path."""
    mirror_dir, cfg = seeded
    _sign_lab_cert(tmp_path, mirror_dir, ["archive.ubuntu.com"])

    net = config.NetworkConfig(name="lab", bundle_dir=tmp_path / "out", certs=True)
    result = bundle_network(net, cfg, mirror_dir)

    applied = tmp_path / "applied-certs"
    _extract(result.out_path, applied)
    tree = _tree(applied)
    assert not any("certs/" in k for k in tree)
    assert all(k.startswith(".mmw/tls/") for k in tree
               if k.endswith((".key", ".csr", ".cnf", ".crt")))
    assert not [k for k in tree if "mirror.key" in k]       # the far side makes its own key


def test_bundle_without_destination_is_an_error(seeded, tmp_path):
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name="lab")
    with pytest.raises(ValueError, match="no bundle destination"):
        bundle_network(net, cfg, mirror_dir)


# --- TLS: publish serves it, a certs network's bundles carry it ------------

def _sign_lab_cert(tmp_path: Path, mirror_dir: Path, hostnames: list[str]) -> Path:
    """A throwaway CA signs a `lab` network key — EC, so it is fast."""
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    from mmw.core import certs

    def pem_key(k):
        return k.private_bytes(serialization.Encoding.PEM,
                               serialization.PrivateFormat.PKCS8,
                               serialization.NoEncryption())

    ca_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Lab CA")])
    now = datetime.now(timezone.utc)
    ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
          .public_key(ca_key.public_key()).serial_number(1)
          .not_valid_before(now - timedelta(days=1))
          .not_valid_after(now + timedelta(days=365))
          .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
          .sign(ca_key, hashes.SHA256()))
    (tmp_path / "ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "ca.key").write_bytes(pem_key(ca_key))

    out = mirror_dir / "certs" / "lab"
    out.mkdir(parents=True)
    key = ec.generate_private_key(ec.SECP256R1())
    (out / "mirror.key").write_bytes(pem_key(key))
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0])]))
           .add_extension(x509.SubjectAlternativeName(
               [x509.DNSName(h) for h in hostnames]), critical=False)
           .sign(key, hashes.SHA256()))
    (out / "mirror.csr").write_bytes(csr.public_bytes(serialization.Encoding.PEM))
    return certs.sign_csr(out / "mirror.csr", tmp_path / "ca.crt", tmp_path / "ca.key")


def test_publish_serves_https_once_signed(seeded, tmp_path):
    mirror_dir, cfg = seeded
    _sign_lab_cert(tmp_path, mirror_dir, ["archive.ubuntu.com"])
    root = tmp_path / "published"
    publish_network(config.NetworkConfig(name="lab", root=root, certs=True), cfg, mirror_dir)
    conf = (root / "nginx" / "mirror.conf").read_text()
    assert "include /srv/mmw/.mmw/tls/https*.conf;" in conf
    https = (root / ".mmw/tls/https.conf").read_text()
    assert "listen 443 ssl;" in https
    assert "ssl_certificate /srv/mmw/.mmw/tls/mirror.crt;" in https
    assert (root / ".mmw/tls/mirror.key").is_file()
    assert not (root / "sign.py").exists() and not (root / "make-ca.py").exists()


def test_publish_without_a_signed_cert_stays_http(seeded, tmp_path):
    mirror_dir, cfg = seeded
    root = tmp_path / "published"
    publish_network(config.NetworkConfig(name="lab", root=root, certs=True), cfg, mirror_dir)
    assert "443" not in (root / "nginx" / "mirror.conf").read_text()


def test_publish_never_wires_a_mismatched_pair(seeded, tmp_path):
    """nginx refuses to start on a cert/key mismatch — the whole server, not
    one site. Keep serving HTTP rather than take everything down."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    mirror_dir, cfg = seeded
    crt = _sign_lab_cert(tmp_path, mirror_dir, ["archive.ubuntu.com"])
    (crt.parent / "mirror.key").write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()))
    root = tmp_path / "published"
    publish_network(config.NetworkConfig(name="lab", root=root, certs=True), cfg, mirror_dir)
    assert "443" not in (root / "nginx" / "mirror.conf").read_text()


def test_dist_upgrade_network_serves_changelogs(seeded, tmp_path):
    mirror_dir, cfg = seeded
    root = tmp_path / "published"
    publish_network(config.NetworkConfig(name="lab", root=root, dist_upgrade=True),
                    cfg, mirror_dir)
    conf = (root / "nginx" / "mirror.conf").read_text()
    assert "server_name changelogs.ubuntu.com;" in conf
    assert "root /srv/mmw/changelogs.ubuntu.com;" in conf


