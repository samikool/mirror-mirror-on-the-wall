"""Headless certs — `mmw certs`, and the hook `commands/publish.py` calls
after a successful `certs = true` publish.

Before this module existed, `NetworkConfig.certs` was only honoured by the
TUI (`tui/publish_flows.py` runs certs after a publish) — cron's
`mmw publish --quiet` never generated TLS material at all, so the same config
produced a cert when a human published and silently produced none from cron.
`generate_for` is the one place that gap is closed, called from both this
command and from `commands/publish.py`.
"""

from pathlib import Path

from mmw.core import disk_io
from mmw.commands.common import eprint, interruptible, load_config_or_none
from mmw.core.config import Config, NetworkConfig

# Imported at module level, not inside the functions below: tests monkeypatch
# this as a module attribute (`certs_cmd.generate_certs = ...`), and an import
# inside a function body would bind a fresh reference to the real one on every
# call, silently defeating the patch. Same reasoning as `commands/bundle.py`'s
# module-level import of `bundle_network`.
from mmw.core.certs import CAKeyEncrypted, cert_dir, check_cert, generate_certs, sign_csr

# Read when the CA key is encrypted and there is no terminal to prompt on.
PASSPHRASE_ENV = "MMW_CA_PASSPHRASE"


def report_cert(out_dir: Path, hostnames: list[str]) -> list[str]:
    """Say whether the signed certificate is fit to serve. Unsigned is a
    normal state (stdout); anything else — stale names, wrong key, expiry —
    goes to stderr, so a cron job that mails only stderr hears about it."""
    problems = check_cert(out_dir, hostnames)
    if problems == ["not signed yet (no mirror.crt)"]:
        print("    TLS: not signed yet — `mmw certs --ca CA.crt --ca-key CA.key`")
    elif problems:
        for p in problems:
            eprint(f"    TLS: {p}")
    else:
        print("    TLS: signed certificate covers every hostname")
    return problems


def generate_for(net: NetworkConfig, cfg: Config) -> list[str]:
    """Generate (or refresh) TLS key/CSR material for one network.

    Returns the hostnames the certificate covers, or `[]` on failure. Never
    raises: this runs after a publish has already succeeded, and a certs
    failure — an unreachable mirror, no hostnames yet resolved — must not be
    allowed to turn a good publish into a failed one.
    """
    try:
        hostnames, key_path, cnf_path, csr_path, md_path = generate_certs(net, cfg)
    except Exception as e:
        eprint(f"Certs for network '{net.name}' failed: {e}")
        return []

    print(f"  Certs for {net.name}: {', '.join(hostnames)}")
    for p in (key_path, cnf_path, csr_path, md_path):
        print(f"    {p}")
    try:
        report_cert(csr_path.parent, hostnames)
    except Exception as e:
        eprint(f"    TLS: could not read the signed certificate: {e}")
    return hostnames


def _passphrase() -> bytes | None:
    import getpass
    import os
    import sys
    env = os.environ.get(PASSPHRASE_ENV)
    if env is not None:
        return env.encode()
    if sys.stdin.isatty():
        return getpass.getpass("CA key passphrase: ").encode()
    return None


@interruptible
def run(cfg_dir: Path = Path("."), network: str | None = None,
        dry_run: bool = False, ca: Path | None = None, ca_key: Path | None = None,
        days: int = 730) -> int:
    """Generate TLS certs for one or all `certs = true` networks. Returns a process exit code."""
    if (ca is None) != (ca_key is None):
        eprint("--ca and --ca-key go together: the CA certificate and its private key.")
        return 1
    for path in (ca, ca_key):
        if path is not None and not disk_io.ops.is_file(Path(path)):
            eprint(f"{path}: no such file")
            return 1

    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1

    if network:
        matches = [n for n in cfg.networks if n.name == network]
        if not matches:
            eprint(f"Network '{network}' not found.")
            return 1
        net = matches[0]
        if not net.certs:
            # A named network without certs=True is a config mismatch worth
            # reporting explicitly rather than exiting 0 having done nothing
            # — the same "report, don't quietly no-op" rule `mmw sync`
            # applies to a repository that resolves to no mirrors.
            eprint(f"Network '{network}' does not have certs = true — nothing to do.")
            return 1
        networks = [net]
    else:
        networks = [n for n in cfg.networks if n.certs]
        if not networks:
            print("No networks have certs = true. Nothing to do.")
            return 0

    if dry_run:
        # This verb writes real RSA key material, and had no preview at all.
        # A dry run must not create any: it names the networks and the
        # destination, and stops there. `mirror.key` is never overwritten by
        # a real run either (regenerating it would invalidate the CA-signed
        # certificate), so the preview says which half would actually change.

        for net in networks:
            dest = cert_dir(net, cfg)
            state = "refresh CSR (key exists)" if disk_io.ops.exists(dest / "mirror.key") \
                else "generate key + CSR"
            if ca is not None:
                state += f", then sign it with {ca}"
            print(f"  [dry-run] {net.name}: would {state} in {dest}")
        print(f"\n  {len(networks)} network(s) — nothing written")
        return 0

    ok = True
    password: bytes | None = None
    for net in networks:
        hostnames = generate_for(net, cfg)
        if not hostnames:
            ok = False
            continue
        if ca is None:
            continue
        csr = cert_dir(net, cfg) / "mirror.csr"
        try:
            try:
                crt = sign_csr(csr, ca, ca_key, days=days, password=password)
            except CAKeyEncrypted:
                password = _passphrase()
                if password is None:
                    eprint(f"{ca_key} is encrypted: run interactively, or set "
                           f"{PASSPHRASE_ENV}.")
                    return 1
                crt = sign_csr(csr, ca, ca_key, days=days, password=password)
        except Exception as e:
            eprint(f"  Signing for network '{net.name}' failed: {e}")
            ok = False
            continue
        print(f"  Signed {crt} ({days} days)")
        if report_cert(crt.parent, hostnames):
            ok = False

    return 0 if ok else 1
