"""Headless bundle — `mmw bundle`, the cron-safe door onto the same pipeline
the Networks tab's Bundle button drives.

Bundling is the one publish-shaped workflow that most wants a cron job: an
airgapped site's operator wants a scheduled `.tar.zst` drop, not a TUI they
have to sit in front of. This module is a thin frontend over
`bundle_network` — same shape as `commands/prune.py` and the headless half of
`mmw publish` in `cli.py`.
"""
import sys
from pathlib import Path

from mmw.core import config
from mmw.core.format import fmt_size as _fmt_size

# Imported at module level, not inside run(): tests monkeypatch this as a
# module attribute (`bundle.bundle_network = ...`), and an import inside the
# function body would bind a fresh reference to the real one every call,
# silently defeating the patch.
from mmw.core.publisher.bundle import bundle_network


def run(cfg_dir: Path = Path("."), network: str | None = None,
        full: bool = False, out: Path | None = None) -> int:
    """Bundle one or all configured networks. Returns a process exit code."""
    try:
        cfg = config.load(cfg_dir)
    except FileNotFoundError as e:
        print(e)
        return 1

    networks = cfg.networks
    if not networks:
        print("No networks configured. Use `mmw manage` to add one.")
        return 1

    if network:
        networks = [n for n in networks if n.name == network]
        if not networks:
            print(f"Network '{network}' not found.", file=sys.stderr)
            return 1

    mirror_dir = cfg.settings.mirror_dir
    ok = True
    for net in networks:
        # A bundle-only destination is `out` (this run) or `net.bundle_dir`
        # (the config). Neither set is not a crash — same as `mmw publish`
        # skipping a network with no `root` — because a multi-network cron
        # run must keep bundling the networks that ARE configured rather than
        # aborting the whole invocation over one that isn't.
        if out is None and net.bundle_dir is None:
            print(f"Skipping network: {net.name} — no bundle destination set "
                  f"(set bundle_dir in mmw.toml or pass --out)")
            ok = False
            continue

        try:
            result = bundle_network(net, cfg, mirror_dir, out_path=out, full=full,
                                     compress=net.bundle_compress)
        except Exception as e:
            print(f"Network '{net.name}' failed: {e}", file=sys.stderr)
            ok = False
            continue

        print(f"{net.name}  →  {result.out_path}")
        print(f"  sent={result.sent} ({_fmt_size(result.sent_bytes)})  deleted={result.deleted}")
        # Reported, never dropped: the TUI surfaces these on the Bundle
        # result notification, and a dist-upgrade warning that is visible to
        # someone watching a modal but silent in cron is exactly the kind of
        # frontend divergence this command exists to close. They are warnings,
        # not failures, so they do not set `ok` — `mmw publish` treats the
        # same list the same way.
        for w in result.dist_upgrade_warnings:
            print(f"    ⚠ {w}", file=sys.stderr)
        if result.errors:
            ok = False
            for e in result.errors:
                print(f"    ✗ {e}", file=sys.stderr)

    return 0 if ok else 1
