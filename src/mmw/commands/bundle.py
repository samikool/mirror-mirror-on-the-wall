"""Headless bundle — `mmw bundle`, the cron-safe door onto the same pipeline
the Networks tab's Bundle button drives.

Bundling is the one publish-shaped workflow that most wants a cron job: an
airgapped site's operator wants a scheduled `.tar` drop, not a TUI they
have to sit in front of. This module is a thin frontend over `bundle_network`
— and deliberately the same shape, check for check, as `commands/publish.py`:
the two verbs' only real difference is the sink.
"""
from pathlib import Path

from mmw.commands.common import eprint, interruptible, load_config_or_none, preflight_ok
from mmw.core.format import fmt_size as _fmt_size

# Imported at module level, not inside run(): tests monkeypatch this as a
# module attribute (`bundle.bundle_network = ...`), and an import inside the
# function body would bind a fresh reference to the real one every call,
# silently defeating the patch.
from mmw.core.publisher.bundle import bundle_network


@interruptible
def run(cfg_dir: Path = Path("."), network: str | None = None,
        full: bool = False, out: Path | None = None,
        allow_incomplete: bool = False, from_ref: str | None = None,
        to_ref: str | None = None) -> int:
    """Bundle one or all configured networks. Returns a process exit code.

    `from_ref` / `to_ref` name snapshots (stamp, label or id) of the one
    network `-n` names; `from_ref="none"` is a full bundle. Unset: from where
    the last bundle went, to a new snapshot of now."""
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1

    networks = cfg.networks
    if not networks:
        eprint("No networks configured. Use `mmw manage` to add one.")
        return 1

    if network:
        networks = [n for n in networks if n.name == network]
        if not networks:
            eprint(f"Network '{network}' not found.")
            return 1

    if (from_ref or to_ref) and not network:
        eprint("--from / --to name a snapshot of one network — pass -n too.")
        return 1
    from mmw.core.publisher.bundle import _LAST
    start = None if (full or from_ref == "none") else (from_ref or _LAST)
    mirror_dir = cfg.settings.mirror_dir
    ok = True
    for net in networks:
        # A bundle destination is `out` (this run) or `net.bundle_dir` (the
        # config). Naming the network with `-n` and finding neither is an
        # error — you asked for something this network cannot do. Meeting it
        # while iterating every network is not: a publish-only network in a
        # bundle run is a normal config, and a multi-network cron run must
        # keep bundling the ones that ARE configured. `mmw publish` applies
        # the identical rule to `root`; the two used to disagree, each
        # claiming parity with the other.
        if out is None and net.bundle_dir is None:
            msg = (f"Network '{net.name}' has no bundle destination set "
                   f"(set bundle_dir in mmw.toml or pass --out)")
            if network:
                eprint(msg)
                return 1
            print(f"Skipping network: {net.name} — no bundle destination set "
                  f"(set bundle_dir in mmw.toml or pass --out)")
            continue

        # The same completeness gate `do_bundle` runs in the TUI, which the
        # headless door never had: a ref whose blob the pool lacks is
        # invisible to `pool.mirror_files()`, so it lands in neither the tar
        # nor the manifest, `apply.py --verify` passes, and the gap surfaces
        # as a 404 during `apt install` on the far side of an airgap.
        # Only "now" is checked here: an existing snapshot's own files are
        # checked by the bundle itself (refused if any left the pool).
        if not to_ref and not preflight_ok("bundle", net, cfg, mirror_dir, allow_incomplete):
            ok = False
            continue

        try:
            result = bundle_network(net, cfg, mirror_dir, out_path=out,
                                    from_ref=start, to_ref=to_ref or "now")
        except Exception as e:
            eprint(f"Network '{net.name}' failed: {e}")
            ok = False
            continue

        span = (f"{result.previous} → {result.created}" if result.previous
                else f"full → {result.created}")
        print(f"{net.name}  {span}  →  {result.out_path}")
        print(f"  sent={result.sent} ({_fmt_size(result.sent_bytes)})  deleted={result.deleted}")
        # The checksum sidecar, when the pipeline produces one. `getattr`
        # rather than an attribute access: `BundleResult` gained this field
        # after this command existed, and a bundle written by an older
        # pipeline has no sidecar to name.
        sha_path = getattr(result, "checksum_path", None)
        if sha_path:
            print(f"  sha256  {sha_path}")
        # Reported, never dropped: the TUI surfaces these on the Bundle
        # result notification, and a dist-upgrade warning that is visible to
        # someone watching a modal but silent in cron is exactly the kind of
        # frontend divergence this command exists to close. They are warnings,
        # not failures, so they do not set `ok` — `mmw publish` treats the
        # same list the same way.
        for w in result.dist_upgrade_warnings:
            eprint(f"    ⚠ {w}")
        if result.errors:
            ok = False
            for e in result.errors:
                eprint(f"    ✗ {e}")

    return 0 if ok else 1
