"""Headless verify — `mmw verify`, the cron-safe door onto the same
completeness check the TUI runs before every publish (the Publish panel's
verify stage).

`pool.mirror_files()` — what both publish and bundle enumerate from — is
`refs ⋈ packages`, so a ref whose blob the pool doesn't hold is *invisible* to
it: an incomplete mirror publishes (or bundles) a tree silently missing files
rather than raising an error, and the gap only surfaces as a 404 during
`apt install` on the far side of an airgap. `verify_mirror` is what catches
this, but before this module it was reachable only by driving
the TUI's verify stage — undoable from cron. This check is
read-only: it never downloads anything.
"""
from pathlib import Path

from mmw.commands.common import eprint, interruptible, load_config_or_none
from mmw.core import resolve
from mmw.core.pool import Pool

# Imported at module level, not inside _verify_all: tests monkeypatch this as
# a module attribute (`verify_cmd.verify_mirror = ...`), and an import inside
# the function body would bind a fresh reference to the real one on every
# call, silently defeating the patch. Same reasoning as `commands/bundle.py`'s
# module-level import of `bundle_network`.
from mmw.core.verify import VerifyResult, verify_mirror


def _verify_all(cfg, mirror_dir: Path, network: str | None) -> list[VerifyResult]:
    """Verify every mirror the pool holds, or one network's slice of them.

    Split out from `run()` so tests can stub this one seam instead of
    constructing a real `Pool` — the pool it opens is read-only, so this is
    safe to run alongside an active sync and creates nothing on disk.
    """
    if network:
        # `run()` has already confirmed the name exists.
        net = next(n for n in cfg.networks if n.name == network)
        mirrors = resolve.network_mirrors(net, cfg, mirror_dir)
    else:
        mirrors = resolve.available_mirrors(cfg, mirror_dir)

    pool = Pool(mirror_dir, read_only=True)
    try:
        return [verify_mirror(m, mirror_dir, pool) for m in mirrors]
    finally:
        pool.close()


@interruptible
def run(cfg_dir: Path = Path("."), network: str | None = None) -> int:
    """Verify pool completeness for one or all mirrors. Returns a process exit code."""
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1

    if network and not any(n.name == network for n in cfg.networks):
        eprint(f"Network '{network}' not found.")
        return 1

    try:
        results = _verify_all(cfg, cfg.settings.mirror_dir, network)
    except OSError as e:
        eprint(f"Cannot read the pool in {cfg.settings.mirror_dir}: {e.strerror or e}")
        return 1

    if not results:
        print("No mirrors to verify.")
        return 0

    ok = True
    for r in results:
        if r.error:
            eprint(f"  {r.mirror_name:<40} ERROR: {r.error}")
            ok = False
        elif r.complete:
            print(f"  {r.mirror_name:<40} complete · {r.total:,} files")
        else:
            eprint(f"  {r.mirror_name:<40} {r.missing_count:,} of {r.total:,} missing")
            ok = False

    complete_count = sum(1 for r in results if r.complete)
    print(f"\n{complete_count}/{len(results)} mirror(s) complete.")

    # A completeness problem is an exit-1 condition — same rule
    # `mmw sync --quiet` documents: a cron job that silently reported nothing
    # wrong on an incomplete pool is worse than one that fails loudly.
    return 0 if ok else 1
