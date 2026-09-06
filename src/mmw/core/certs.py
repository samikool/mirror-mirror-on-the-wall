from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from mmw.core.config import Config, NetworkConfig
from mmw.core.resolve import network_mirrors


def collect_hostnames(network: NetworkConfig, cfg: Config) -> list[str]:
    """Return sorted unique hostnames for every mirror in the network."""
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
    return sorted(hostnames)


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
    else:
        key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
        key_path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        key_path.chmod(0o600)

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
    cert_path = out_dir / "mirror.crt"
    hosts_block = "\n".join(f"  - {h}" for h in hostnames)

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

These files live outside the published tree (`{network.root}`) on purpose —
never move the private key anywhere a web server can reach.

## Step 1 — Transfer the CSR to your CA machine

```
scp {csr_path} user@ca-machine:/tmp/mirror.csr
```

## Step 2 — Sign it on the CA machine

```bash
openssl x509 -req \\
  -in /tmp/mirror.csr \\
  -CA houdini.crt \\
  -CAkey houdini.key \\
  -CAcreateserial \\
  -out /tmp/mirror.crt \\
  -days 730 \\
  -copy_extensions copy
```

Requires OpenSSL 1.1.1+.  `-copy_extensions copy` preserves the SANs from the CSR.

## Step 3 — Transfer the signed cert back

```
scp user@ca-machine:/tmp/mirror.crt {cert_path}
```

## Step 4 — Configure your reverse proxy

Point your reverse proxy at `{cert_path}` (cert) and `{key_path}` (key).

Since all published files live under `{network.root}/{{hostname}}/`, a single
server block using the `$host` variable as part of the document root covers
every hostname without per-host configuration:

```nginx
# Single block — serves all mirrored hostnames
server {{
    listen 443 ssl;
    server_name {' '.join(hostnames)};

    ssl_certificate     {cert_path};
    ssl_certificate_key {key_path};

    root {network.root}/$host;
    autoindex on;
}}
```

Reload nginx after placing the cert:  `nginx -t && systemctl reload nginx`

## Regenerating the CSR

If you edit `{cnf_path.name}` (e.g. to add or remove hostnames), regenerate the CSR
without touching the private key:

```bash
openssl req -new \\
  -key {key_path} \\
  -out {csr_path} \\
  -config {cnf_path}
```

Then repeat steps 1–3 to get a new signed cert from your CA.
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
    return cfg.settings.mirror_dir / "certs" / network.name


def generate_certs(network: NetworkConfig, cfg: Config) -> tuple[list[str], Path, Path, Path, Path]:
    """Top-level entry point.  Returns (hostnames, key_path, cnf_path, csr_path, md_path)."""
    out_dir = cert_dir(network, cfg)
    hostnames = collect_hostnames(network, cfg)
    if not hostnames:
        raise ValueError("No hostnames found — network has no mirrors configured")
    key_path, cnf_path, csr_path = generate_key_and_csr(hostnames, out_dir)
    md_path = write_instructions(network, hostnames, key_path, cnf_path, csr_path, out_dir)
    return hostnames, key_path, cnf_path, csr_path, md_path
