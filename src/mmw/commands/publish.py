"""Headless publish — `mmw publish --quiet` / `--dry-run`. Same shape as
`commands/bundle.py`, its direct sibling over the same `[[networks]]` config;
this one drives `publish_network`/`dry_run_network` (TreeSink) instead of
`bundle_network` (BundleSink).
"""
import sys
from pathlib import Path

from mmw.core import config

# Imported at module level, not inside run(): tests monkeypatch these as
# module attributes (`publish.publish_network = ...`), and an import inside
# the function body would bind a fresh reference to the real one every call,
# silently defeating the patch.
from mmw.core.publisher import dry_run_network, publish_network
from mmw.commands.certs import generate_for


def run(cfg_dir: Path = Path("."), network: str | None = None, dry_run: bool = False) -> int:
    """Publish one or all configured networks. Returns a process exit code."""
    try:
        cfg = config.load(cfg_dir)
    except FileNotFoundError as e:
        print(e)
        return 1

    networks = cfg.networks
    if not networks:
        print("No networks configured. Use `mmw publish` (TUI) to add one.")
        return 1

    if network:
        networks = [n for n in networks if n.name == network]
        if not networks:
            print(f"Network '{network}' not found.", file=sys.stderr)
            return 1

    label = "[dry-run] " if dry_run else ""
    ok = True
    for net in networks:
        if net.root is None:
            # A bundle-only network — publish_base() has no destination
            # to derive, and printing `net.root` unconditionally below
            # used to print the literal string "None" for exactly this
            # case. `dry_run_network`/`publish_network` both already
            # return a graceful per-network error instead of raising, but
            # skip here too so an `mmw publish` iterating several
            # networks never even calls into either for one that can only
            # be bundled.
            print(
                f"\n{label}Skipping network: {net.name} — no publish root "
                f"set (use `mmw bundle` instead)"
            )
            continue

        print(f"\n{label}Publishing network: {net.name}  →  {net.root}")
        try:
            if dry_run:
                result = dry_run_network(net, cfg, cfg.settings.mirror_dir)
            else:
                result = publish_network(net, cfg, cfg.settings.mirror_dir)
        except Exception as e:
            # Caught here, not left to propagate: an uncaught exception used
            # to abort the whole invocation, so a network that broke halfway
            # through a multi-network cron run took every network after it
            # down too, printing a traceback where a mailed stderr line would
            # do. Matches `commands/bundle.py`'s same guard for the same
            # reason.
            print(f"Network '{net.name}' failed: {e}", file=sys.stderr)
            ok = False
            continue

        for mr in result.mirrors:
            status = f"  {mr.mirror_name:<40} linked={mr.linked}  skipped={mr.skipped}"
            if mr.errors:
                status += f"  errors={len(mr.errors)}"
            print(status)
            for e in mr.errors:
                print(f"    ✗ {e}", file=sys.stderr)
        # A publish that reported errors is a failed publish. This used to
        # exit 0 regardless — the old inline body had no exit-code logic at
        # all, so `mmw publish --quiet` printing "✗ boom: disk full" still
        # looked like success to the cron job that ran it. That is the same
        # failure mode `mmw sync --quiet` is explicitly documented against
        # ("exit 1 on package OR metadata failure"), and the same one
        # `commands/bundle.py` already guards with this exact check; publish
        # was the one verb that silently didn't.
        if result.errors:
            ok = False
        # Reported, never dropped: the TUI surfaces these on the publish
        # result notification, and a dist-upgrade warning that is visible to
        # someone watching a modal but silent in cron is exactly the kind of
        # frontend divergence this command exists to close. They are
        # warnings, not failures, so they never set `ok`.
        for w in result.dist_upgrade_warnings:
            print(f"    ⚠ {w}", file=sys.stderr)

        # `net.certs` is the TUI's cue too (publish_panel._execute_publish
        # calls `_run_certs` after a clean publish) — headless never called
        # it at all, so the same config produced a cert from the TUI and none
        # from cron. Skipped on a dry run: generating real key material is
        # exactly the kind of write a dry run promises not to make.
        if net.certs and not dry_run:
            generate_for(net, cfg)

        print(
            f"\n  Total: {result.linked} {'would link' if dry_run else 'linked'}, "
            f"{result.skipped} already current"
            + (f", {len(result.errors)} error(s)" if result.errors else "")
        )

    return 0 if ok else 1
