from pathlib import Path

from mmw.core import config
from mmw.core.format import fmt_size as _fmt_size
from mmw.core.pool import Pool


def run(cfg_dir: Path = Path(".")) -> None:
    try:
        cfg = config.load(cfg_dir)
    except FileNotFoundError as e:
        print(e)
        raise SystemExit(1)
    mirror_dir = cfg.settings.mirror_dir

    # Keyed on the repository, not on mirror names: `arches = []` means "all",
    # so the configured mirror list is narrower than what was actually synced.
    configured = [s.identity for s in cfg.sources]
    pool = Pool(mirror_dir)
    try:
        print(f"Pruning pool against {len(configured)} configured repositor(y/ies)...")
        removed, freed = pool.prune_orphans(configured)
    finally:
        pool.close()

    if removed == 0:
        print("Pool is clean — no orphaned packages found.")
    else:
        print(f"Removed {removed:,} orphaned package(s), freed {_fmt_size(freed)}.")
