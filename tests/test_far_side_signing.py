"""The CA lives on the airgapped side: a certs network bundles with no
certificate, sign.py signs every mirror's request there (real openssl), and
apply.py turns HTTPS on once a certificate fits the key."""
import ast
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mmw.core import config
from mmw.core.publisher.bundle import bundle_network

from test_bundle import seeded  # noqa: F401 — the shared pool fixture

SIGN = Path(__file__).parent.parent / "src" / "mmw" / "resources" / "sign.py"
needs_openssl = pytest.mark.skipif(shutil.which("openssl") is None,
                                   reason="requires the openssl binary")


def _ca(tmp_path: Path, passphrase: bytes | None = None) -> tuple[Path, Path]:
    """A throwaway CA (EC, so it is fast)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Far CA")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                           critical=False)
            .sign(key, hashes.SHA256()))
    enc = (serialization.BestAvailableEncryption(passphrase) if passphrase
           else serialization.NoEncryption())
    crt, k = tmp_path / "ca.crt", tmp_path / "ca.key"
    crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    k.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                    serialization.PrivateFormat.PKCS8, enc))
    return crt, k


def _bundled_and_applied(seeded, tmp_path, name="lab"):  # noqa: F811
    """A certs network bundled with no certificate, applied at a far root."""
    mirror_dir, cfg = seeded
    net = config.NetworkConfig(name=name, bundle_dir=tmp_path / "out", certs=True)
    result = bundle_network(net, cfg, mirror_dir)
    root = tmp_path / "far" / name
    proc = subprocess.run([sys.executable, str(tmp_path / "out" / "apply.py"),
                           str(result.out_path), "--root", str(root)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    return root, proc.stdout


def _run(script: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(script), *map(str, args)],
                          capture_output=True, text=True)


def test_sign_script_needs_only_python_and_openssl():
    tree = ast.parse(SIGN.read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"argparse", "getpass", "os", "re", "shutil", "subprocess",
                        "sys", "tempfile"}
    assert not any(isinstance(n, ast.JoinedStr) for n in ast.walk(tree)), \
        "f-strings are avoided in far-side scripts (Python 3.6, by convention)"


@needs_openssl
def test_sign_on_the_far_side_then_https_turns_on(seeded, tmp_path):  # noqa: F811
    root, applied = _bundled_and_applied(seeded, tmp_path)
    tls = root / ".mmw" / "tls"
    assert "HTTPS waits for a signed certificate" in applied
    assert not (tls / "https.conf").exists()

    ca, ca_key = _ca(tmp_path)
    collected = tmp_path / "npm"
    signed = _run(tmp_path / "out" / "sign.py", "--ca", ca, "--ca-key", ca_key,
                  tmp_path / "far", "--out", collected)       # a directory of roots
    assert signed.returncode == 0, signed.stderr
    assert "lab" in signed.stdout and "signed (no certificate yet)" in signed.stdout
    assert f"apply.py --tls --root {root}" in signed.stdout

    # The certificate verifies against the CA and covers the request's hosts.
    subprocess.run(["openssl", "verify", "-CAfile", str(ca), str(tls / "mirror.crt")],
                   check=True, capture_output=True)
    text = subprocess.run(["openssl", "x509", "-in", str(tls / "mirror.crt"), "-noout",
                           "-text"], capture_output=True, text=True, check=True).stdout
    assert "DNS:archive.ubuntu.com" in text and "TLS Web Server Authentication" in text

    # Collected for Nginx Proxy Manager: cert, key (0600) and the CA.
    assert (collected / "lab.crt").read_bytes() == (tls / "mirror.crt").read_bytes()
    assert (collected / "lab.key").read_bytes() == (tls / "mirror.key").read_bytes()
    assert stat.S_IMODE(os.stat(collected / "lab.key").st_mode) == 0o600
    assert (collected / "ca.crt").read_bytes() == ca.read_bytes()

    on = _run(tmp_path / "out" / "apply.py", "--tls", "--root", root)
    assert "HTTPS on — reload nginx" in on.stdout
    https = (tls / "https.conf").read_text()
    assert "listen 443 ssl;" in https and f"ssl_certificate {tls}/mirror.crt;" in https
    assert f"include {tls}/https*.conf;" in (root / "nginx" / "mirror.conf").read_text()

    again = _run(tmp_path / "out" / "sign.py", "--ca", ca, "--ca-key", ca_key, root)
    assert "current — left alone" in again.stdout


@needs_openssl
def test_a_later_bundle_keeps_the_far_sides_certificate(seeded, tmp_path):  # noqa: F811
    """The bundle never carries a certificate it does not have, so the one
    signed on the far side survives every later apply — HTTPS stays on."""
    root, _ = _bundled_and_applied(seeded, tmp_path)
    ca, ca_key = _ca(tmp_path)
    assert _run(tmp_path / "out" / "sign.py", "--ca", ca, "--ca-key", ca_key,
                root).returncode == 0
    crt = (root / ".mmw" / "tls" / "mirror.crt").read_bytes()
    _, applied = _bundled_and_applied(seeded, tmp_path)
    assert (root / ".mmw" / "tls" / "mirror.crt").read_bytes() == crt
    assert "HTTPS on" in applied


@needs_openssl
def test_an_encrypted_ca_key_asks_once(seeded, tmp_path, monkeypatch):  # noqa: F811
    root, _ = _bundled_and_applied(seeded, tmp_path)
    ca, ca_key = _ca(tmp_path, passphrase=b"s3cret")
    ns = {"__name__": "sign"}
    exec(compile(SIGN.read_text(), str(SIGN), "exec"), ns)
    asked = []
    monkeypatch.setattr(ns["getpass"], "getpass", lambda prompt: asked.append(prompt) or "s3cret")
    assert ns["main"](["--ca", str(ca), "--ca-key", str(ca_key), str(root)]) == 0
    assert len(asked) == 1 and (root / ".mmw" / "tls" / "mirror.crt").exists()


@needs_openssl
def test_the_wrong_ca_key_is_refused_before_signing_anything(seeded, tmp_path):  # noqa: F811
    root, _ = _bundled_and_applied(seeded, tmp_path)
    ca, _ = _ca(tmp_path)
    other = tmp_path / "other"
    other.mkdir()
    _, other_key = _ca(other)
    r = _run(tmp_path / "out" / "sign.py", "--ca", ca, "--ca-key", other_key, root)
    assert r.returncode == 1 and "is not the key of" in r.stderr
    assert not (root / ".mmw" / "tls" / "mirror.crt").exists()


def test_a_certificate_for_another_key_keeps_https_off(seeded, tmp_path):  # noqa: F811
    """nginx refuses to start on a mismatched pair — the whole server."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    root, _ = _bundled_and_applied(seeded, tmp_path)
    stranger = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "archive.ubuntu.com")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(stranger.public_key()).serial_number(2)
            .not_valid_before(now).not_valid_after(now + timedelta(days=30))
            .sign(stranger, hashes.SHA256()))
    tls = root / ".mmw" / "tls"
    (tls / "mirror.crt").write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    r = _run(tmp_path / "out" / "apply.py", "--tls", "--root", root)
    assert "HTTPS off: mirror.crt does not fit mirror.key" in r.stdout
    assert not (tls / "https.conf").exists()


@needs_openssl
def test_a_certificate_missing_a_new_hostname_is_reported(seeded, tmp_path):  # noqa: F811
    """A repository added later brings a hostname the installed certificate
    does not cover: apply.py says so, and sign.py re-signs it."""
    root, _ = _bundled_and_applied(seeded, tmp_path)
    ca, ca_key = _ca(tmp_path)
    assert _run(tmp_path / "out" / "sign.py", "--ca", ca, "--ca-key", ca_key,
                root).returncode == 0
    tls = root / ".mmw" / "tls"
    (tls / "mirror.cnf").write_text((tls / "mirror.cnf").read_text()
                                    + "DNS.99 = new.example.com\n")
    r = _run(tmp_path / "out" / "apply.py", "--tls", "--root", root)
    assert "does not cover new.example.com" in r.stdout
