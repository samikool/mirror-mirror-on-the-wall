"""The far side's TLS: the operator's CA, recorded by path (the key is never
copied and its passphrase never kept), and a key and certificate per root
for exactly the hostnames that root serves."""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from mmw.core import disk_io
from mmw.core.certs import _load_ca
from mmw.site.home import load_site, save_settings

TRUST = """\
Next: install {crt} — never the key — on every client:
  Ubuntu/Debian:  sudo cp {crt} /usr/local/share/ca-certificates/site-ca.crt && sudo update-ca-certificates
  RHEL/Fedora:    sudo cp {crt} /etc/pki/ca-trust/source/anchors/site-ca.crt && sudo update-ca-trust
  Chrome / VS Code on Linux:  certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n "{name}" -i {crt}
  Firefox:        Settings > Certificates > Authorities > Import
  Windows:        open {crt} > Install > Local Machine > Trusted Root Certification Authorities
  macOS:          sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain {crt}
Keep {key} private and backed up: losing it means re-trusting a new CA everywhere.
Then `mmw site certs` signs every root."""


def load_ca(home: Path, cert: Path, key: Path, password: bytes | None) -> str:
    """Check the CA (a CA certificate, and its own key) and record where it
    is. The subject, for people. `CAKeyEncrypted` when a passphrase is needed."""
    cert, key = Path(cert).resolve(), Path(key).resolve()
    ca_cert, _ca_key = _load_ca(cert, key, password)
    save_settings(home, load_site(home).model_copy(update={"ca_cert": cert, "ca_key": key}))
    return ca_cert.subject.rfc4514_string()


def make_ca(out: Path, name: str, password: bytes | None) -> tuple[Path, Path]:
    """A new self-signed CA at `out/ca.crt` and `out/ca.key` (0600) — never
    over an existing one."""
    out = Path(out)
    crt, key_path = out / "ca.crt", out / "ca.key"
    if disk_io.ops.exists(crt) or disk_io.ops.exists(key_path):
        raise FileExistsError(f"a CA is already at {out} — a CA is made once; load it instead")
    disk_io.ops.mkdir(out, parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=False, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False, key_agreement=False,
                                         key_cert_sign=True, crl_sign=True, encipher_only=False,
                                         decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    enc = (serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption())
    pem_key = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, enc)
    fd = disk_io.ops.open_fd(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)   # 0600 from the start
    with disk_io.ops.open_file(fd, "wb") as fh:
        fh.write(pem_key)
    disk_io.ops.write_bytes(crt, cert.public_bytes(serialization.Encoding.PEM))
    return crt, key_path


def show_ca(home: Path) -> tuple[str, datetime, Path, Path] | None:
    s = load_site(home)
    if s.ca_cert is None:
        return None
    cert = x509.load_pem_x509_certificate(disk_io.ops.read_bytes(s.ca_cert))
    return cert.subject.rfc4514_string(), cert.not_valid_after_utc, s.ca_cert, s.ca_key
