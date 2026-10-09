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
- **`--dry-run`** answers "what would go" without deleting. The preview runs
  the same set difference against a read-only connection; it never writes.
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


def _preview(mirror_dir: Path, configured: list[str]) -> tuple[int, int]:
    """(files, bytes) `prune_orphans(configured)` would remove. Read-only.

    Models exactly what `Pool.prune_orphans` does — drop the refs of
    unconfigured repositories, then count the packages no remaining ref wants
    — but in one SELECT against a `mode=ro` connection, so a preview can never
    be the thing that deletes. Kept here rather than in `core/pool.py` so the
    read-only query lives beside the command that needs it.
    """
    db_path = mirror_dir / ".pool" / "mmw.db"
    if not db_path.exists():
        return 0, 0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(refs)")}
        if "source" in cols:
            ph = ",".join("?" * len(configured))
            keep = f"(r.source = '' OR r.source IN ({ph}))"
            params = configured
        else:
            # A pre-`source` pool can prove nothing about any ref, and
            # `prune_orphans` never deletes those rows either.
            keep, params = "1", []
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM packages "
            "WHERE NOT EXISTS (SELECT 1 FROM refs r WHERE r.checksum = packages.checksum "
            f"AND {keep})",
            params,
        ).fetchone()
        return int(row[0]), int(row[1])
    finally:
        conn.close()


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
        removable, freeable = _preview(mirror_dir, configured)
    except (OSError, sqlite3.Error) as e:
        eprint(f"Cannot read the pool in {mirror_dir}: {e}")
        return 1

    print(f"Pruning pool against {len(configured)} configured repositor(y/ies)...")
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
        removed, freed = pool.prune_orphans(configured)
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
