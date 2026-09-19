"""Headless certs — `mmw certs`, and the hook `commands/publish.py` calls
after a successful `certs = true` publish.

Before this module existed, `NetworkConfig.certs` was only honoured by the
TUI (`tui/publish_panel.py` calls `_run_certs(net)` after a publish) — cron's
`mmw publish --quiet` never generated TLS material at all, so the same config
produced a cert when a human published and silently produced none from cron.
`generate_for` is the one place that gap is closed, called from both this
command and from `commands/publish.py`.
"""
import sys
from pathlib import Path

from mmw.core import config
from mmw.core.config import Config, NetworkConfig

# Imported at module level, not inside the functions below: tests monkeypatch
# this as a module attribute (`certs_cmd.generate_certs = ...`), and an import
# inside a function body would bind a fresh reference to the real one on every
# call, silently defeating the patch. Same reasoning as `commands/bundle.py`'s
# module-level import of `bundle_network`.
from mmw.core.certs import generate_certs


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
        print(f"Certs for network '{net.name}' failed: {e}", file=sys.stderr)
        return []

    print(f"  Certs for {net.name}: {', '.join(hostnames)}")
    for p in (key_path, cnf_path, csr_path, md_path):
        print(f"    {p}")
    return hostnames


def run(cfg_dir: Path = Path("."), network: str | None = None) -> int:
    """Generate TLS certs for one or all `certs = true` networks. Returns a process exit code."""
    try:
        cfg = config.load(cfg_dir)
    except FileNotFoundError as e:
        print(e)
        return 1

    if network:
        matches = [n for n in cfg.networks if n.name == network]
        if not matches:
            print(f"Network '{network}' not found.", file=sys.stderr)
            return 1
        net = matches[0]
        if not net.certs:
            # A named network without certs=True is a config mismatch worth
            # reporting explicitly rather than exiting 0 having done nothing
            # — the same "report, don't quietly no-op" rule `mmw sync`
            # applies to a repository that resolves to no mirrors.
            print(f"Network '{network}' does not have certs = true — nothing to do.", file=sys.stderr)
            return 1
        networks = [net]
    else:
        networks = [n for n in cfg.networks if n.certs]
        if not networks:
            print("No networks have certs = true. Nothing to do.")
            return 0

    ok = True
    for net in networks:
        if not generate_for(net, cfg):
            ok = False

    return 0 if ok else 1
