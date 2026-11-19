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


def _plan(cfg, mirror_dir: Path, released: list[int] = ()) -> tuple[list[str], int, int]:
    """(dropped mirrors, files, bytes) a prune would remove. Read-only: the
    preview is `Pool.prune_preview` — the prune's own orphan query, with the
    refs it would delete filtered out rather than deleted — on a read-only
    pool, so a preview can never be the thing that deletes. `released`: the
    points retention drops first — their files count when nothing else keeps them."""
    from mmw.core.resolve import uncovered_mirror_names
    with Pool(mirror_dir, read_only=True) as pool:
        dropped = uncovered_mirror_names(cfg, pool)
        files, size = pool.prune_preview([s.identity for s in cfg.sources], dropped, released)
        gone = sorted(n for n in pool.network_names() - {n.name for n in cfg.networks}
                      if pool.snapshots(n))
    for name in gone:
        # Snapshots are GC roots, so a network removed from mmw.toml by hand
        # keeps its files in the pool until its snapshots go. Said here, where
        # someone is asking what can be freed.
        print(f"  Legacy snapshot data for {name} remains protected until its point conversion "
              "is complete; inspect repository history with `mmw history`.")
    return dropped, files, size


@interruptible
def costs_of_keeping(cfg, mirror_dir: Path, presets=(1, 3, 10, "all")) -> dict[str, list[tuple]]:
    """Per network: (choice, points dropped, bytes freed) for each `keep_points`
    preset and its own setting — what that network's choice frees, against
    it keeping everything, with the other networks as configured."""
    from mmw.core import points
    configured = [s.identity for s in cfg.sources]
    out: dict[str, list[tuple]] = {}
    with Pool(mirror_dir, read_only=True) as pool:
        from mmw.core.resolve import uncovered_mirror_names
        dropped = uncovered_mirror_names(cfg, pool)

        def frees(net, choice) -> tuple[int, int]:
            nets = [n.model_copy(update={"keep_points": choice}) if n.name == net.name else n
                    for n in cfg.networks]
            plan = points.retention_plan(pool, nets, cfg=cfg)
            return len(plan), pool.prune_preview(configured, dropped, plan)[1]
        for net in cfg.networks:
            n_all, base = frees(net, "all")
            rows = []
            for choice in dict.fromkeys([*presets, net.keep_points]):
                n, size = (n_all, base) if choice == "all" else frees(net, choice)
                rows.append((choice, n - n_all, size - base))
            out[net.name] = rows
        for source in cfg.sources:
            if not hasattr(source, "keep_points"):
                continue
            own = source.keep_points if source.keep_points is not None else cfg.settings.keep_points

            def repo_frees(choice):
                selected = cfg.model_copy(update={"sources": [
                    s.model_copy(update={"keep_points": choice}) if s.identity == source.identity else s
                    for s in cfg.sources]})
                plan = points.retention_plan(pool, cfg.networks, cfg=selected)
                return len(plan), pool.prune_preview(configured, dropped, plan)[1]
            n_all, base = repo_frees("all")
            rows = []
            for choice in dict.fromkeys([*presets, own]):
                n, size = (n_all, base) if choice == "all" else repo_frees(choice)
                rows.append((choice, n - n_all, size - base))
            out[f"repository {source.name}"] = rows
    return out


def run(cfg_dir: Path = Path("."), dry_run: bool = False, yes: bool = False, costs: bool = False) -> int:
    """Remove pool blobs no configured repository's refs want. Exit code.
    `costs`: a preview (implies `dry_run`) of what each keep_points choice frees."""
    dry_run = dry_run or costs
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

    # Old timeline points no network keeps: counted here (read-only), dropped
    # only once the prune goes ahead — under its lock, so their files free now.
    from mmw.core import points
    from mmw.core.pool import Pool
    try:
        with Pool(mirror_dir, read_only=True) as pool:
            releasing = points.retention_plan(pool, cfg.networks, cfg=cfg)
    except sqlite3.OperationalError:
        releasing = []                          # a pool from before points: none to drop
    except (OSError, sqlite3.Error) as e:
        eprint(f"Cannot read the pool in {mirror_dir}: {e}")
        return 1

    dropping = len(releasing)
    try:
        dropped, removable, freeable = _plan(cfg, mirror_dir, releasing)
    except (OSError, sqlite3.Error) as e:
        eprint(f"Cannot read the pool in {mirror_dir}: {e}")
        return 1

    print(f"Pruning pool against {len(configured)} configured repositor(y/ies)...")
    if dropped:
        print(f"  {len(dropped)} synced mirror(s) are no longer in the config "
              f"(a suite, component or arch was removed): "
              f"{', '.join(dropped[:5])}{' …' if len(dropped) > 5 else ''}")
    if dropping:
        print(f"  {dropping:,} old point(s) no retention rule keeps (keep_points) "
              f"{'would be' if dry_run else 'will be'} dropped — their files freed with the rest")
    if dry_run and costs:
        try:
            table = costs_of_keeping(cfg, mirror_dir)
        except sqlite3.OperationalError:
            table = {}
            print("  No points recorded yet — keep_points has nothing to choose between.")
        except (OSError, sqlite3.Error) as e:
            eprint(f"Cannot read the pool in {mirror_dir}: {e}")
            return 1
        if table:
            print("  What each network and repository keep_points choice frees, against keeping all "
                  "(the other retention rules as set):")
        for name, rows in table.items():
            net = settings_for_costs(cfg)[name]
            said = " · ".join(f"{c}: {_fmt_size(b)}" + (" (now)" if c == net.keep_points else "")
                              for c, _n, b in rows)
            print(f"    {name}  {said}")
            if net.keep_suites:
                print(f"    {' ' * len(name)}  (suites set on their own keep their setting: "
                      f"{', '.join(f'{k} {v}' for k, v in net.keep_suites.items())})")
    if dry_run:
        if removable == 0:
            print("Pool is clean — no orphaned packages found. (dry run)")
        else:
            print(f"Would remove {removable:,} orphaned package(s), "
                  f"freeing {_fmt_size(freeable)}. (dry run — nothing deleted)")
        return 0

    if removable == 0 and not dropping:
        print("Pool is clean — no orphaned packages found.")
        return 0

    if not yes and sys.stdin.isatty():
        also = f" and drop {dropping:,} old point(s)" if dropping else ""
        answer = input(
            f"Remove {removable:,} orphaned package(s), freeing {_fmt_size(freeable)}{also}? [y/N] "
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
            progress=lambda done, total: print(f"  {done:,} of {total:,} blobs removed…", flush=True),
            before=lambda locked: points.apply_retention(locked, cfg.networks, cfg=cfg))
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


def settings_for_costs(cfg):
    """Labels and effective rules shared by CLI and TUI cost presentations."""
    from types import SimpleNamespace
    return {**{n.name: n for n in cfg.networks},
            **{f"repository {s.name}": SimpleNamespace(
                keep_points=s.keep_points if s.keep_points is not None else cfg.settings.keep_points,
                keep_suites=s.keep_suites) for s in cfg.sources if hasattr(s, "keep_points")}}
