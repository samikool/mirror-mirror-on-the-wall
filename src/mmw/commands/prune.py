"""Headless pool prune — `mmw prune`.

The **only** destructive CLI verb, and until this rewrite the only one with no
preview, no confirmation and no guard:

- **Zero configured sources deleted the entire pool.** `prune_orphans([])`
  takes the `else` branch `DELETE FROM refs WHERE source != ''`, after which
  every blob is an orphan and every one of them is unlinked. A config whose
  `[[sources]]` had just been edited out — or a `--config` pointed at the
  wrong directory — silently wiped a multi-terabyte pool while printing
  "Pruning pool against 0 configured repositor(y/ies)...". An empty
  configured-source list cannot *prove* anything is unwanted, which is the
  same standard `prune_orphans` already applies to pre-`source` refs rows, so
  it is refused outright here.
- **`--dry-run`** answers "what would go" without deleting. The preview is
  the prune's own orphan query (`Pool.prune_preview`) on a read-only pool; it
  never writes, and it cannot drift from what the prune actually removes.
- **`--yes`** skips the confirmation an interactive run now gets. A
  non-interactive run (cron) proceeds without asking, because a prompt nobody
  can answer is just a hang.
"""
import sqlite3
import sys
from pathlib import Path

from mmw.commands.common import eprint, interruptible, load_config_or_none
from mmw.core.format import fmt_size as _fmt_size
from mmw.core.pool import Pool, PoolBusy


def _plan(cfg, mirror_dir: Path) -> tuple[list[str], int, int]:
    """(dropped mirrors, files, bytes) a prune would remove. Read-only: the
    preview is `Pool.prune_preview` — the prune's own orphan query, with the
    refs it would delete filtered out rather than deleted — on a read-only
    pool, so a preview can never be the thing that deletes."""
    from mmw.core.resolve import uncovered_mirror_names
    with Pool(mirror_dir, read_only=True) as pool:
        dropped = uncovered_mirror_names(cfg, pool)
        files, size = pool.prune_preview([s.identity for s in cfg.sources], dropped)
        gone = sorted(n for n in pool.network_names() - {n.name for n in cfg.networks}
                      if pool.snapshots(n))
    for name in gone:
        # Snapshots are GC roots, so a network removed from mmw.toml by hand
        # keeps its files in the pool until its snapshots go. Said here, where
        # someone is asking what can be freed.
        print(f"  Snapshots of {name} (no longer configured) still hold files — "
              f"`mmw snapshot clean -n {name} --keep 0 --yes` lets prune free them.")
    return dropped, files, size


@interruptible
def run(cfg_dir: Path = Path("."), dry_run: bool = False, yes: bool = False) -> int:
    """Remove pool blobs no configured repository's refs want. Exit code."""
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1
    mirror_dir = cfg.settings.mirror_dir

    # Keyed on the repository, not on mirror names: `arches = []` means "all",
    # so the configured mirror list is narrower than what was actually synced.
    configured = [s.identity for s in cfg.sources]
    if not configured:
        eprint(
            f"Refusing to prune: no [[sources]] are configured in "
            f"{cfg_dir}/mmw.toml.\n"
            "  With nothing configured, every ref in the pool looks unwanted and "
            "prune would delete the whole pool.\n"
            "  Add the repositories back (or point --config at the right "
            "directory) before pruning."
        )
        return 1

    try:
        dropped, removable, freeable = _plan(cfg, mirror_dir)
    except (OSError, sqlite3.Error) as e:
        eprint(f"Cannot read the pool in {mirror_dir}: {e}")
        return 1

    print(f"Pruning pool against {len(configured)} configured repositor(y/ies)...")
    if dropped:
        print(f"  {len(dropped)} synced mirror(s) are no longer in the config "
              f"(a suite, component or arch was removed): "
              f"{', '.join(dropped[:5])}{' …' if len(dropped) > 5 else ''}")
    if dry_run:
        if removable == 0:
            print("Pool is clean — no orphaned packages found. (dry run)")
        else:
            print(f"Would remove {removable:,} orphaned package(s), "
                  f"freeing {_fmt_size(freeable)}. (dry run — nothing deleted)")
        return 0

    if removable == 0:
        print("Pool is clean — no orphaned packages found.")
        return 0

    if not yes and sys.stdin.isatty():
        answer = input(
            f"Remove {removable:,} orphaned package(s), freeing {_fmt_size(freeable)}? [y/N] "
        )
        if answer.strip().lower() not in ("y", "yes"):
            print("Aborted — nothing was deleted.")
            return 1

    try:
        pool = Pool(mirror_dir)
    except OSError as e:
        eprint(f"Cannot open the pool in {mirror_dir}: {e.strerror or e}")
        return 1
    try:
        removed, freed = pool.prune_orphans(
            configured, dropped,
            progress=lambda done, total: print(f"  {done:,} of {total:,} blobs removed…", flush=True))
    except PoolBusy as e:
        # A sync is running. Until a mirror's refs are reconciled at the end of
        # its run, every blob it has downloaded is `packages − refs` — i.e.
        # indistinguishable from an orphan — so pruning now would delete the
        # sync's own work out from under it.
        eprint(str(e))
        return 1
    finally:
        pool.close()

    if removed == 0:
        print("Pool is clean — no orphaned packages found.")
    else:
        print(f"Removed {removed:,} orphaned package(s), freed {_fmt_size(freed)}.")
    return 0
