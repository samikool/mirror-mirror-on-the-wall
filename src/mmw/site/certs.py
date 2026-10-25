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


def _ca_paths(s) -> tuple[Path, Path] | None:
    """Where the CA is: this machine's record (never a root's), or a v1 site's."""
    from mmw.site import recents
    got = recents.ca_paths()
    if got is not None:
        return got
    return (s.ca_cert, s.ca_key) if s is not None and s.ca_cert is not None else None


def load_ca(cert: Path, key: Path, password: bytes | None) -> str:
    """Check the CA (a CA certificate, and its own key) and record where it
    is — on this machine, never in a root. The subject, for people.
    `CAKeyEncrypted` when a passphrase is needed."""
    from mmw.site import recents
    cert, key = Path(cert).resolve(), Path(key).resolve()
    ca_cert, _ca_key = _load_ca(cert, key, password)
    recents.set_ca_paths(cert, key)
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


def show_ca() -> tuple[str, datetime, Path, Path] | None:
    paths = _ca_paths(None)
    if paths is None:
        return None
    cert = x509.load_pem_x509_certificate(disk_io.ops.read_bytes(paths[0]))
    return cert.subject.rfc4514_string(), cert.not_valid_after_utc, *paths


# ── a certificate per root ───────────────────────────────────────────────────

KEY_MADE_HERE = "key-made-here"


class RootCertResult:
    def __init__(self, root: str, hostnames: list[str], action: str, notes: list[str]):
        self.root, self.hostnames, self.action, self.notes = root, hostnames, action, notes


def root_hostnames(root: Path) -> list[str]:
    """The DNS names a root serves: its nginx's `server_name`s."""
    import re
    from mmw.core.publisher.nginx import _dns_name
    conf = Path(root) / "nginx" / "mirror.conf"
    if not disk_io.ops.exists(conf):
        return []
    names: set[str] = set()
    for line in disk_io.ops.read_text(conf).splitlines():
        m = re.match(r"\s*server_name\s+(.+?);", line)
        if m:
            names.update(_dns_name(n) for n in m.group(1).split())
    return sorted(n for n in names if n and n != "_")


def sign_roots(home: Path, *, password: bytes | None, days: int = 730,
               force: bool = False) -> list[RootCertResult]:
    """Give every mounted root a key (kept if it has one), a request for its
    hostnames, and a certificate from the site's CA — unless the one it has
    still fits, covers them and is not about to expire. Then HTTPS follows
    (`https.conf` only once the certificate fits the key)."""
    from mmw.site.lock import SiteLock
    s = load_site(home)
    paths = _ca_paths(s)
    if paths is None:
        raise ValueError("no CA yet — mmw site ca load --cert … --key …, or mmw site ca make --out …")
    for path in paths:
        if not disk_io.ops.exists(path):
            raise FileNotFoundError(f"the CA isn't at {path} any more (moved, or not mounted?)")
    ca_cert, _ca_key = _load_ca(*paths, password)   # wrong passphrase / mismatch: first
    s = s.model_copy(update={"ca_cert": paths[0], "ca_key": paths[1]})
    with SiteLock(home):
        out = [_sign_root(r, ca_cert, s, password, days, force) for r in s.roots]
    if s.name is not None:
        for r in out:
            r.root = s.name                     # a v2 root is called by its own name
    return out


def sign_set(mset, *, password: bytes | None, days: int = 730, force: bool = False) -> list[RootCertResult]:
    """Sign every root of the set that is here."""
    return [r for root in mset.roots for r in sign_roots(root.home, password=password, days=days, force=force)]


def _good(tls: Path, hosts: list[str], ca_cert) -> bool:
    """The certificate there still fits, covers `hosts`, isn't about to
    expire — and is the site CA's."""
    from mmw.core.certs import check_cert
    try:
        if check_cert(tls, hosts):
            return False
        leaf = x509.load_pem_x509_certificates(disk_io.ops.read_bytes(tls / "mirror.crt"))[0]
    except (ValueError, OSError):
        return False                                   # unreadable: sign a good one
    return leaf.issuer == ca_cert.subject


def _sign_root(r, ca_cert, s, password, days, force) -> "RootCertResult":
    from mmw.core.certs import generate_key_and_csr, sign_csr
    from mmw.resources.apply import https_state
    root = Path(r.path)
    if not disk_io.ops.is_dir(root / ".mmw" / "store"):
        return RootCertResult(r.name, [], "not mounted", [])
    hosts = root_hostnames(root)
    if not hosts:
        return RootCertResult(r.name, [], "no hostnames", [])
    tls = root / ".mmw" / "tls"
    if disk_io.ops.is_symlink(tls):
        raise ValueError(f"{tls} is a symlink — refusing to write a private key through it")
    disk_io.ops.mkdir(tls, parents=True, exist_ok=True)
    disk_io.ops.chmod(tls, 0o700)
    notes = []
    if disk_io.ops.exists(tls / "mirror.key") and not disk_io.ops.exists(tls / KEY_MADE_HERE):
        # A key an older bundle carried over (made on the connected side):
        # replaced once, so this root's key never crossed on transfer media.
        for name in ("mirror.key", "mirror.crt", "mirror.csr"):
            disk_io.ops.unlink(tls / name, missing_ok=True)
        notes.append("replaced the key that came over in an older bundle with one made here")
    _key, _cnf, csr = generate_key_and_csr(hosts, tls)
    disk_io.ops.write_text(tls / KEY_MADE_HERE, "mirror.key was made on this side\n")
    if not force and _good(tls, hosts, ca_cert):
        action = "kept"
    else:
        sign_csr(csr, s.ca_cert, s.ca_key, days=days, password=password)
        action = "signed"
    return RootCertResult(r.name, hosts, action, notes + https_state(str(root)))


def tls_notes(home: Path) -> list[str]:
    """Per root: a certificate that no longer covers what the root serves, a
    root serving hostnames with no certificate while others have one (or a
    CA is loaded), HTTPS on or off to match. Never raises: a bad file is a
    note."""
    from mmw.core.certs import check_cert
    from mmw.resources.apply import https_state
    s = load_site(home)
    roots = [r for r in s.roots if disk_io.ops.is_dir(Path(r.path) / ".mmw" / "store")]
    signed = {r.name for r in roots if disk_io.ops.exists(Path(r.path) / ".mmw" / "tls" / "mirror.crt")}
    in_use = bool(signed) or _ca_paths(s) is not None
    notes = []
    for r in roots:
        root = Path(r.path)
        tls = root / ".mmw" / "tls"
        try:
            hosts = root_hostnames(root)
            if r.name not in signed:
                if in_use and hosts:
                    notes.append(f"{r.name}: no certificate yet for {len(hosts)} hostname(s) — mmw site certs")
                continue
            for problem in check_cert(tls, hosts):
                notes.append(f"{r.name}: {problem.replace('re-sign', 'mmw site certs')}")
            notes.extend(f"{r.name}: {n}" for n in https_state(str(root)) if "sign.py" not in n)
        except (ValueError, OSError) as e:
            notes.append(f"{r.name}: its certificate or key can't be read ({e}) — mmw site certs")
    return notes
