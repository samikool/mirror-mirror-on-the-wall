import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from mmw.core.config import NETWORK_NAME_RE, Config, NetworkConfig
from mmw.core.resolve import network_mirrors


# Published beside the mirrors by a `dist_upgrade` network (see
# publisher/dist_upgrade.py), and fetched over https by a stock Ubuntu client's
# release-upgrade check — so it needs to be on the certificate too.
CHANGELOGS_HOST = "changelogs.ubuntu.com"

# `check_cert` warns this long before expiry: enough to get a re-signed cert
# across an airgap before clients start refusing the old one.
EXPIRY_WARNING = timedelta(days=30)


def collect_hostnames(network: NetworkConfig, cfg: Config) -> list[str]:
    """Return sorted unique hostnames the network serves: every mirror's host,
    plus changelogs.ubuntu.com for a `dist_upgrade` network."""
    selected_mirrors = network_mirrors(network, cfg, cfg.settings.mirror_dir)
    mirror_map = {m.name: m for m in selected_mirrors}
    selected = list(mirror_map)
    seen: set[str] = set()
    hostnames: list[str] = []
    for name in selected:
        mirror = mirror_map.get(name)
        if mirror is None:
            continue
        host = urlparse(mirror.source_url).hostname
        if host and host not in seen:
            seen.add(host)
            hostnames.append(host)
    if network.dist_upgrade and CHANGELOGS_HOST not in seen:
        hostnames.append(CHANGELOGS_HOST)
        seen.add(CHANGELOGS_HOST)
    # Keys published at their upstream path are served from their own host,
    # which need not be any mirror's (nvidia.github.io/libnvidia-container/
    # gpgkey sits outside the repository it signs).
    from mmw.core.publisher.links import upstream_files, upstream_file_hosts
    upstream_paths = upstream_files(cfg.settings.mirror_dir, list(mirror_map.values()))
    for dir_name in upstream_file_hosts(upstream_paths):
        host = _real_host(dir_name)
        if host not in seen:
            seen.add(host)
            hostnames.append(host)
    return sorted(hostnames)


def _real_host(dir_name: str) -> str:
    """`host_8080` (a published directory name) → `host`, the DNS name."""
    head, sep, tail = dir_name.rpartition("_")
    return head if sep and tail.isdigit() else dir_name


def write_cnf(hostnames: list[str], out_dir: Path) -> Path:
    """Write an OpenSSL config file covering all hostnames as SANs."""
    alt_names = "\n".join(f"DNS.{i+1} = {h}" for i, h in enumerate(hostnames))
    content = f"""\
[req]
default_bits       = 4096
prompt             = no
default_md         = sha256
distinguished_name = dn
req_extensions     = req_ext

[dn]
CN = {hostnames[0]}

[req_ext]
subjectAltName = @alt_names

[alt_names]
{alt_names}
"""
    cnf_path = out_dir / "mirror.cnf"
    cnf_path.write_text(content)
    return cnf_path


def generate_key_and_csr(hostnames: list[str], out_dir: Path) -> tuple[Path, Path, Path]:
    """Generate (or reuse) a 4096-bit RSA private key, an OpenSSL config, and a CSR.

    An existing mirror.key is NEVER overwritten — regenerating the key would
    invalidate any certificate the CA already signed.  Hostname changes only
    require a fresh CSR, which is always rewritten from the current key.

    Returns (key_path, cnf_path, csr_path).  Key is written 0600; others world-readable.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    key_path = out_dir / "mirror.key"
    if key_path.exists():
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
        # Unconditional, not only on the write path: a key created before this
        # (or restored from a backup, or copied with `cp`) keeps whatever mode
        # the umask gave it forever, and nothing else ever revisits the file.
        key_path.chmod(0o600)
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
        pem = key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        # `write_bytes()` then `chmod()` creates the file 0644 under a typical
        # umask and leaves it readable for as long as the write takes — and
        # generating RSA-4096 means that window is not instantaneous. Create it
        # 0600 in the first place, and O_EXCL so a concurrent generator cannot
        # be the one that made it.
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(pem)
        key_path.chmod(0o600)          # defeat a permissive umask on some platforms

    cnf_path = write_cnf(hostnames, out_dir)

    san_list = [x509.DNSName(h) for h in hostnames]
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostnames[0])]))
        .add_extension(x509.SubjectAlternativeName(san_list), critical=False)
        .sign(key, hashes.SHA256())
    )
    csr_path = out_dir / "mirror.csr"
    csr_path.write_bytes(csr.public_bytes(serialization.Encoding.PEM))

    return key_path, cnf_path, csr_path


def write_instructions(
    network: NetworkConfig,
    hostnames: list[str],
    key_path: Path,
    cnf_path: Path,
    csr_path: Path,
    out_dir: Path,
) -> Path:
    hosts_block = "\n".join(f"  - {h}" for h in hostnames)
    # certs are network-scoped, but a network with no publish root yet (bundle-
    # only, applied elsewhere) has no root to name here — see
    # write_upgrade_md's identical guard in publisher/dist_upgrade.py.
    root_desc = f"`{network.root}`" if network.root else "the directory this publish tree is extracted to"

    content = f"""\
# TLS Certificate — {network.name}

Generated: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}

## Hostnames covered ({len(hostnames)})

{hosts_block}

## Files

| File | Purpose |
|------|---------|
| `{key_path.name}` | Private key — stays on this server, never transfer |
| `{cnf_path.name}` | OpenSSL config — edit this to adjust hostnames or key size |
| `{csr_path.name}` | Certificate signing request — send this to your CA |
| `mirror.crt` | Signed certificate — put here after signing |

These files live outside the published tree ({root_desc}) on purpose —
never move the private key anywhere a web server can reach.

## Sign it

If this machine can read the CA's certificate and key:

```bash
mmw certs -n {network.name} --ca ca.crt --ca-key ca.key
```

That writes `mirror.crt` here, and the next `mmw publish` / `mmw bundle`
serves every hostname above over HTTPS as well as HTTP — the generated
`nginx/mirror.conf` gains `listen 443 ssl` and the certificate paths on its
own. Nothing to hand-edit; edits to the generated file are overwritten anyway.

If the CA lives on another machine, sign the CSR there instead (OpenSSL 3.0+
for `-copy_extensions`) and put the result here as `mirror.crt`:

```bash
openssl x509 -req -in mirror.csr -CA ca.crt -CAkey ca.key -CAcreateserial \\
  -out mirror.crt -days 730 -copy_extensions copy
```

`mmw certs` re-checks the certificate on every run and says when it no longer
covers the network's hostnames, no longer matches `mirror.key`, or is about to
expire.

## Bundled networks

The far side's nginx needs the certificate and key too. Set
`bundle_tls = true` on the network to carry both inside every bundle (to
`{{root}}/.mmw/tls/`, which no generated server block serves). That puts the
private key on your transfer media — it is off unless you turn it on.

## Regenerating the CSR

If you edit `{cnf_path.name}` (e.g. to add or remove hostnames), regenerate the CSR
without touching the private key:

```bash
openssl req -new \\
  -key {key_path} \\
  -out {csr_path} \\
  -config {cnf_path}
```

Then sign it again (see **Sign it** above).
"""

    md_path = out_dir / "CERTS.md"
    md_path.write_text(content)
    return md_path


def cert_dir(network: NetworkConfig, cfg: Config) -> Path:
    """Where key material lives for a network.

    Deliberately OUTSIDE network.root: the publish root is served by a web
    server (often with autoindex on), and a private key must never be one
    misconfigured location block away from being public.
    """
    # `NetworkConfig` validates the name as a path segment, so this cannot
    # escape — the check is repeated because interpolating an unvalidated name
    # into a path is exactly how `name = "../../../tmp/pwn"` used to write a
    # private key outside the mirror directory, and a model built with
    # `model_construct()` skips validators.
    if not NETWORK_NAME_RE.fullmatch(network.name or "") or network.name in (".", ".."):
        raise ValueError(f"invalid network name {network.name!r}")
    return cfg.settings.mirror_dir / "certs" / network.name



class CAKeyEncrypted(ValueError):
    """The CA private key needs a passphrase and none was given."""


def _load_ca(ca_cert_path: Path, ca_key_path: Path, password: bytes | None):
    ca_cert = x509.load_pem_x509_certificate(Path(ca_cert_path).read_bytes())
    try:
        ca_key = serialization.load_pem_private_key(
            Path(ca_key_path).read_bytes(), password=password)
    except TypeError as e:
        # cryptography raises TypeError both for "encrypted, no password" and
        # "password given, key not encrypted" — only the first is worth a
        # prompt, and it is the only one that can happen with password=None.
        if password is None:
            raise CAKeyEncrypted(f"{ca_key_path} is encrypted — a passphrase is needed") from e
        raise ValueError(f"{ca_key_path}: {e}") from e

    try:
        is_ca = ca_cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    except x509.ExtensionNotFound:
        is_ca = False
    if not is_ca:
        raise ValueError(f"{ca_cert_path} is not a CA certificate (basicConstraints CA:FALSE)")

    def _spki(k):
        return k.public_bytes(serialization.Encoding.DER,
                              serialization.PublicFormat.SubjectPublicKeyInfo)
    if _spki(ca_key.public_key()) != _spki(ca_cert.public_key()):
        raise ValueError(f"{ca_key_path} does not match {ca_cert_path}")
    return ca_cert, ca_key


def sign_csr(csr_path: Path, ca_cert_path: Path, ca_key_path: Path, *,
             days: int = 730, password: bytes | None = None) -> Path:
    """Sign a network's `mirror.csr` with the site CA → `mirror.crt` beside it.

    The certificate is a plain TLS server certificate for exactly the names
    the CSR asks for. When the CA is an intermediate (not self-signed), its
    certificate is appended: clients trust the root, and nginx has to send the
    intermediate for them to build a path to it. A root is never appended.

    Written via a temp file and rename, so nginx never reads a half-written
    certificate.
    """
    csr_path = Path(csr_path)
    csr = x509.load_pem_x509_csr(csr_path.read_bytes())
    if not csr.is_signature_valid:
        raise ValueError(f"{csr_path}: CSR signature does not verify")
    ca_cert, ca_key = _load_ca(ca_cert_path, ca_key_path, password)

    try:
        san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        raise ValueError(f"{csr_path}: CSR names no hostnames (no subjectAltName)") from None

    now = datetime.now(timezone.utc)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(csr.subject)
        .issuer_name(ca_cert.subject)
        .public_key(csr.public_key())
        .serial_number(x509.random_serial_number())
        # A few minutes' slack: a far-side clock slightly behind this one
        # must not see a certificate that is "not yet valid".
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=days))
        .add_extension(san, critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.KeyUsage(
            digital_signature=True, key_encipherment=True, content_commitment=False,
            data_encipherment=False, key_agreement=False, key_cert_sign=False,
            crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                       critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(csr.public_key()),
                       critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
            ca_cert.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    pem = leaf.public_bytes(serialization.Encoding.PEM)
    if ca_cert.issuer != ca_cert.subject:
        pem += ca_cert.public_bytes(serialization.Encoding.PEM)

    out = csr_path.parent / "mirror.crt"
    tmp = out.with_suffix(".crt.tmp")
    tmp.write_bytes(pem)
    tmp.chmod(0o644)
    tmp.replace(out)
    return out


def _key_matches(out_dir: Path) -> bool:
    """Whether `mirror.crt` (its leaf) is for `mirror.key`. nginx refuses to
    start on a mismatched pair — not just that site, the whole server."""
    leaf = x509.load_pem_x509_certificates((out_dir / "mirror.crt").read_bytes())[0]
    key = serialization.load_pem_private_key((out_dir / "mirror.key").read_bytes(),
                                             password=None)
    fmt = (serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return key.public_key().public_bytes(*fmt) == leaf.public_key().public_bytes(*fmt)


def check_cert(out_dir: Path, hostnames: list[str]) -> list[str]:
    """Problems with the signed certificate in `out_dir`, as sentences.

    `[]` means it is safe to serve: it exists, matches `mirror.key`, covers
    every hostname the network serves now, and is not about to expire. Each
    of those has gone wrong silently before something checked it — a network
    gaining a host rewrites the CSR but never the signed certificate.
    """
    out_dir = Path(out_dir)
    crt_path = out_dir / "mirror.crt"
    if not crt_path.exists():
        return ["not signed yet (no mirror.crt)"]
    leaf = x509.load_pem_x509_certificates(crt_path.read_bytes())[0]
    problems: list[str] = []

    if (out_dir / "mirror.key").exists() and not _key_matches(out_dir):
        problems.append("certificate is for a different key than mirror.key — re-sign")

    try:
        covered = set(leaf.extensions.get_extension_for_class(
            x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName))
    except x509.ExtensionNotFound:
        covered = set()
    missing = [h for h in hostnames if h not in covered]
    if missing:
        problems.append(f"certificate does not cover {', '.join(missing)} — re-sign")

    left = leaf.not_valid_after_utc - datetime.now(timezone.utc)
    if left <= timedelta(0):
        problems.append(f"certificate expired {leaf.not_valid_after_utc:%Y-%m-%d} — re-sign")
    elif left <= EXPIRY_WARNING:
        problems.append(f"certificate expires {leaf.not_valid_after_utc:%Y-%m-%d} — re-sign soon")
    return problems


def signed_paths(network: NetworkConfig, cfg: Config) -> tuple[Path, Path] | None:
    """`(mirror.crt, mirror.key)` when the network has a signed certificate
    to serve, else None. Only `certs = true` networks ever do."""
    if not network.certs:
        return None
    d = cert_dir(network, cfg)
    crt, key = d / "mirror.crt", d / "mirror.key"
    return (crt, key) if crt.exists() and key.exists() else None


def servable_paths(network: NetworkConfig, cfg: Config) -> tuple[Path, Path] | None:
    """`signed_paths`, but only when nginx can actually load the pair.

    A mismatched pair takes the whole nginx server down, so it is never
    wired in — the site keeps serving plain HTTP, and `mmw certs` says why.
    Stale names or a near expiry are NOT reasons to withhold it: HTTPS for
    the covered names still works, and those are reported, not blocking.
    """
    paths = signed_paths(network, cfg)
    if paths is None:
        return None
    try:
        return paths if _key_matches(paths[0].parent) else None
    except (OSError, ValueError):
        return None

def generate_certs(network: NetworkConfig, cfg: Config) -> tuple[list[str], Path, Path, Path, Path]:
    """Top-level entry point.  Returns (hostnames, key_path, cnf_path, csr_path, md_path)."""
    out_dir = cert_dir(network, cfg)
    hostnames = collect_hostnames(network, cfg)
    if not hostnames:
        raise ValueError("No hostnames found — network has no mirrors configured")
    key_path, cnf_path, csr_path = generate_key_and_csr(hostnames, out_dir)
    md_path = write_instructions(network, hostnames, key_path, cnf_path, csr_path, out_dir)
    return hostnames, key_path, cnf_path, csr_path, md_path
