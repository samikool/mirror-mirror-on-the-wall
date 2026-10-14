import asyncio
import concurrent.futures
import time
from pathlib import Path

import typer

from mmw.commands.common import eprint, interruptible, load_config_or_none
from mmw.core import config
from mmw.core.format import fmt_size as _fmt_size
from mmw.core.pool import Pool
from mmw.commands.mirror import resolve_and_report
from mmw.core.sync_engine import classify_mirror
from mmw.core.item_progress import ItemProgressSession


def _pct(part: int, total: int) -> str:
    if total == 0:
        return "  0%"
    return f"{part / total * 100:3.0f}%"


def _bar(part: int, total: int, width: int = 24) -> str:
    if total == 0:
        return "░" * width
    filled = round(part / total * width)
    return "█" * filled + "░" * (width - filled)


W = 60


def _rule(char: str = "─") -> str:
    return char * W


async def _check_all(cfg: config.Config) -> int:
    """Report what a sync would fetch. Returns a process exit code."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=cfg.settings.max_connections)
    try:
        return await _check_all_inner(cfg, executor)
    finally:
        # `finally`, so an exception or a Ctrl-C can't strand the workers.
        executor.shutdown(wait=False)


async def _check_all_inner(cfg: config.Config, executor) -> int:
    mirror_dir = cfg.settings.mirror_dir

    # Counted, not merely printed. Before this only `resolve_errors` reached
    # the exit gate, so a run where every metadata fetch failed printed
    # "ERROR fetching metadata" per mirror, then a confident
    # `0 packages / 0.0 B` total, and exited 0.
    mirror_errors: list[str] = []

    grand_pkgs = grand_size = grand_fetch_n = grand_fetch_size = grand_pool_n = grand_pool_size = 0

    typer.echo()
    typer.echo(_rule("═"))
    mirrors, resolve_errors = resolve_and_report(cfg)
    items = ItemProgressSession(mirrors)
    typer.echo(f"  mmw dry-run  —  {len(mirrors)} mirror(s)")
    typer.echo(f"  mirror dir : {mirror_dir}")
    typer.echo(_rule("═"))

    try:
        pool_cm = Pool(mirror_dir, read_only=True)
    except OSError as e:
        eprint(f"Cannot open the pool in {mirror_dir}: {e.strerror or e}")
        return 1

    with pool_cm as pool:
        for mirror in mirrors:
            typer.echo()
            typer.echo(f"  ▶  {mirror.name}  [{mirror.provider}]")
            typer.echo(_rule())

            typer.echo("  fetching metadata...")
            t0 = time.monotonic()
            try:
                stats = await classify_mirror(mirror, pool, executor)
            except Exception as e:
                typer.echo(f"  ERROR fetching metadata: {e}", err=True)
                mirror_errors.append(f"{mirror.name}: {e}")
                continue
            typer.echo(f"  {stats.total:,} {stats.item_label}  ({time.monotonic() - t0:.1f}s)")

            grand_pkgs       += stats.total
            grand_size       += stats.total_size
            grand_fetch_n    += stats.to_fetch
            grand_fetch_size += stats.fetch_size
            grand_pool_n     += stats.in_pool
            grand_pool_size  += stats.pool_size
            if stats.inventory is not None:
                await items.classify_async(mirror.name, stats.inventory)

            col = 18
            typer.echo(f"  {stats.item_label.capitalize():<{col}} {stats.total:>8,}")
            if stats.sizes_known:
                typer.echo(f"  {'Full size':<{col}} {_fmt_size(stats.total_size):>10}")
            else:
                typer.echo(f"  {'Full size':<{col}} {'unknown':>10}")

            typer.echo()
            typer.echo(f"  {'In pool':<{col}} {stats.in_pool:>8,}  {_pct(stats.in_pool, stats.total)}  {_fmt_size(stats.pool_size)}")
            typer.echo(f"  {'To download':<{col}} {stats.to_fetch:>8,}  {_pct(stats.to_fetch, stats.total)}  {_fmt_size(stats.fetch_size) if stats.sizes_known else '?'}")
            typer.echo()

            typer.echo(f"  pool    {_bar(stats.in_pool, stats.total)}  {_pct(stats.in_pool, stats.total)}")
            typer.echo(f"  fetch   {_bar(stats.to_fetch, stats.total)}  {_pct(stats.to_fetch, stats.total)}")

            if stats.no_checksum:
                typer.echo()
                typer.echo(f"  ⚠  {stats.no_checksum} file(s) have no known checksum —")
                typer.echo("     size and pool status unknown until download.")

        typer.echo()
        typer.echo(_rule("═"))
        typer.echo("  TOTALS")
        typer.echo(_rule("═"))

        pool_stats = pool.stats()
        if items.counts:
            totals = items.totals
            grand_pkgs, grand_pool_n, grand_fetch_n = totals.total, totals.pooled, totals.downloaded
        label = items.totals.label
        col = 22
        typer.echo(f"  {'Mirrors checked':<{col}} {len(mirrors):,}")
        typer.echo(f"  {'Total ' + label:<{col}} {grand_pkgs:,}")
        typer.echo(f"  {'Total repo size':<{col}} {_fmt_size(grand_size)}")
        typer.echo()
        typer.echo(f"  {'Already in pool':<{col}} {grand_pool_n:,}  ({_fmt_size(grand_pool_size)} saved)")
        typer.echo(f"  {'To download':<{col}} {grand_fetch_n:,}  ({_fmt_size(grand_fetch_size)})")
        typer.echo()
        typer.echo(f"  {'Pool (all mirrors)':<{col}} {pool_stats['packages']:,} files  /  {_fmt_size(pool_stats['total_size'])}")
        typer.echo(_rule("═"))
        typer.echo()

    # A repository that could not be listed contributed no mirrors, and a
    # mirror whose index could not be read was checked against nothing — a
    # dry-run that silently checked nothing must not look like success.
    if mirror_errors:
        eprint(f"\n  {len(mirror_errors)} mirror(s) could not be checked: "
               + ", ".join(mirror_errors))
    if resolve_errors or mirror_errors:
        return 1
    return 0


@interruptible
def run(cfg_dir: Path = Path(".")) -> int:
    """Show what would be downloaded without fetching anything."""
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1

    return asyncio.run(_check_all(cfg))
