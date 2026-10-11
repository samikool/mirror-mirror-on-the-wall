"""Headless publish — `mmw publish --quiet` / `--dry-run`. Same shape as
`commands/bundle.py`, its direct sibling over the same `[[networks]]` config;
this one drives `publish_network`/`dry_run_network` (TreeSink) instead of
`bundle_network` (BundleSink).

The two are deliberately structurally identical, down to the order of the
checks, because every place they drifted apart was a bug: publish exited 0
when a named network could not be published while bundle exited 1 for the
mirror image of the same situation, and neither ran the completeness gate the
TUI has always run (`commands/common.preflight_ok`).
"""
from pathlib import Path

from mmw.commands.common import eprint, interruptible, load_config_or_none, preflight_ok

# Imported at module level, not inside run(): tests monkeypatch these as
# module attributes (`publish.publish_network = ...`), and an import inside
# the function body would bind a fresh reference to the real one every call,
# silently defeating the patch.
from mmw.core.format import fmt_size
from mmw.core.publisher import dry_run_network, publish_network, publish_snapshot
from mmw.commands.certs import generate_for


@interruptible
def run(cfg_dir: Path = Path("."), network: str | None = None, dry_run: bool = False,
        allow_incomplete: bool = False, snapshot: str | None = None,
        root: Path | None = None) -> int:
    """Publish one or all configured networks. Returns a process exit code.

    `snapshot`: publish that snapshot (stamp, label or id) instead of the
    live state ("live" is the live state). `root`: into this directory
    instead of the network's root — its nginx config points there, with the
    same hostnames, so it needs its own address."""
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1
    if snapshot == "live":
        snapshot = None
    if (snapshot or root) and not network:
        eprint("--snapshot and --root publish one network — name it with -n.")
        return 1
    if snapshot and dry_run:
        eprint("--dry-run estimates the live state only — drop --snapshot.")
        return 1

    networks = cfg.networks
    if not networks:
        eprint("No networks configured. Use `mmw publish` (TUI) to add one.")
        return 1

    if network:
        networks = [n for n in networks if n.name == network]
        if not networks:
            eprint(f"Network '{network}' not found.")
            return 1

    label = "[dry-run] " if dry_run else ""
    ok = True
    for net in networks:
        if root is not None:
            net = net.model_copy(update={"root": root})
        if net.root is None:
            # A bundle-only network — publish_base() has no destination to
            # derive, and printing `net.root` unconditionally below used to
            # print the literal string "None" for exactly this case.
            #
            # Naming it with `-n` is an error (you asked for something this
            # network cannot do); meeting it while iterating every network is
            # not (a bundle-only network in a publish run is a normal config).
            # `mmw bundle` applies the identical rule to `bundle_dir`.
            msg = (f"{label}Network '{net.name}' has no publish root set "
                   f"(set root in mmw.toml, or use `mmw bundle`)")
            if network:
                eprint(msg)
                return 1
            print(f"\n{label}Skipping network: {net.name} — no publish root "
                  f"set (use `mmw bundle` instead)")
            continue

        # The gate `tui/publish_flows.PublishFlows._run_pre_publish` has always run and the
        # headless path never did. `pool.mirror_files()` is `refs ⋈ packages`,
        # so a ref whose blob the pool lacks is invisible to it — and the
        # destination **prune** then runs against that under-complete keep set
        # and deletes correct files out of a live tree.
        # A snapshot's files are checked by publish_snapshot itself: the gate
        # below is about the live state's refs.
        if not snapshot and not preflight_ok("publish", net, cfg, cfg.settings.mirror_dir,
                                             allow_incomplete, warn_only=dry_run):
            ok = False
            continue

        what = f"snapshot {snapshot} of " if snapshot else ""
        print(f"\n{label}Publishing {what}network: {net.name}  →  {net.root}")
        try:
            if snapshot:
                result = publish_snapshot(net, cfg, cfg.settings.mirror_dir, snapshot,
                                          root=net.root)
            elif dry_run:
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
            eprint(f"Network '{net.name}' failed: {e}")
            ok = False
            continue

        for mr in result.mirrors:
            status = f"  {mr.mirror_name:<40} linked={mr.linked}  skipped={mr.skipped}"
            if mr.errors:
                status += f"  errors={len(mr.errors)}"
            print(status)
            for e in mr.errors:
                eprint(f"    ✗ {e}")
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
            eprint(f"    ⚠ {w}")

        # `net.certs` is the TUI's cue too (publish_flows.PublishFlows._execute_publish
        # calls `_run_certs` after a clean publish) — headless never called
        # it at all, so the same config produced a cert from the TUI and none
        # from cron. Skipped on a dry run: generating real key material is
        # exactly the kind of write a dry run promises not to make.
        if net.certs and not dry_run:
            generate_for(net, cfg)

        sizes = (f" ({fmt_size(result.linked_bytes)} of {fmt_size(result.total_bytes)}; "
                 f"each file counted once)" if dry_run else "")
        print(
            f"\n  Total: {result.linked} {'would link' if dry_run else 'linked'}, "
            f"{result.skipped} already current{sizes}"
            + (f", {len(result.errors)} error(s)" if result.errors else "")
        )

    return 0 if ok else 1
