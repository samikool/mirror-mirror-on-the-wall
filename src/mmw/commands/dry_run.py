import asyncio
import concurrent.futures
import time

import typer

from mmw.core import config
from mmw.core.format import fmt_size as _fmt_size
from mmw.core.pool import Pool
from mmw.commands.mirror import resolve_and_report
from mmw.core.downloader import fetch_packages_threaded
from mmw.providers import get_provider


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


async def _check_all(cfg: config.Config):
    mirror_dir = cfg.settings.mirror_dir
    loop = asyncio.get_running_loop()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=cfg.settings.max_connections)

    grand_pkgs = grand_size = grand_fetch_n = grand_fetch_size = grand_pool_n = grand_pool_size = 0

    typer.echo()
    typer.echo(_rule("═"))
    mirrors, resolve_errors = resolve_and_report(cfg)
    typer.echo(f"  mmw dry-run  —  {len(mirrors)} mirror(s)")
    typer.echo(f"  mirror dir : {mirror_dir}")
    typer.echo(_rule("═"))

    with Pool(mirror_dir, read_only=True) as pool:
        for mirror in mirrors:
            typer.echo()
            typer.echo(f"  ▶  {mirror.name}  [{mirror.provider}]")
            typer.echo(_rule())

            try:
                provider = get_provider(mirror.provider)
            except ValueError as e:
                typer.echo(f"  ERROR: {e}", err=True)
                continue

            typer.echo("  fetching metadata...")
            t0 = time.monotonic()
            try:
                packages = await loop.run_in_executor(
                    executor, fetch_packages_threaded, provider, mirror
                )
            except Exception as e:
                typer.echo(f"  ERROR fetching metadata: {e}", err=True)
                continue
            typer.echo(f"  {len(packages):,} packages  ({time.monotonic() - t0:.1f}s)")

            known = [p.checksum for p in packages if p.checksum]
            pooled = await loop.run_in_executor(executor, pool.batch_contains, known)
            in_pool   = [p for p in packages if p.checksum and p.checksum in pooled]
            to_fetch  = [p for p in packages if not p.checksum or p.checksum not in pooled]
            size_known = all(p.size > 0 for p in packages)

            total_size  = sum(p.size for p in packages)
            pool_size   = sum(p.size for p in in_pool)
            fetch_size  = sum(p.size for p in to_fetch)
            unknown_n   = sum(1 for p in to_fetch if not p.checksum)

            grand_pkgs       += len(packages)
            grand_size       += total_size
            grand_fetch_n    += len(to_fetch)
            grand_fetch_size += fetch_size
            grand_pool_n     += len(in_pool)
            grand_pool_size  += pool_size

            col = 18
            typer.echo(f"  {'Packages':<{col}} {len(packages):>8,}")
            if size_known:
                typer.echo(f"  {'Full size':<{col}} {_fmt_size(total_size):>10}")
            else:
                typer.echo(f"  {'Full size':<{col}} {'unknown':>10}")

            typer.echo()
            typer.echo(f"  {'In pool':<{col}} {len(in_pool):>8,}  {_pct(len(in_pool), len(packages))}  {_fmt_size(pool_size)}")
            typer.echo(f"  {'To download':<{col}} {len(to_fetch):>8,}  {_pct(len(to_fetch), len(packages))}  {_fmt_size(fetch_size) if size_known else '?'}")
            typer.echo()

            bar_pool  = _bar(len(in_pool), len(packages))
            bar_fetch = _bar(len(to_fetch), len(packages))
            typer.echo(f"  pool    {bar_pool}  {_pct(len(in_pool), len(packages))}")
            typer.echo(f"  fetch   {bar_fetch}  {_pct(len(to_fetch), len(packages))}")

            if unknown_n:
                typer.echo()
                typer.echo(f"  ⚠  {unknown_n} package(s) have no known checksum —")
                typer.echo("     size and pool status unknown until download.")

        typer.echo()
        typer.echo(_rule("═"))
        typer.echo("  TOTALS")
        typer.echo(_rule("═"))

        pool_stats = pool.stats()
        col = 22
        typer.echo(f"  {'Mirrors checked':<{col}} {len(mirrors):,}")
        typer.echo(f"  {'Total packages':<{col}} {grand_pkgs:,}")
        typer.echo(f"  {'Total repo size':<{col}} {_fmt_size(grand_size)}")
        typer.echo()
        typer.echo(f"  {'Already in pool':<{col}} {grand_pool_n:,}  ({_fmt_size(grand_pool_size)} saved)")
        typer.echo(f"  {'To download':<{col}} {grand_fetch_n:,}  ({_fmt_size(grand_fetch_size)})")
        typer.echo()
        typer.echo(f"  {'Pool (all mirrors)':<{col}} {pool_stats['packages']:,} packages  /  {_fmt_size(pool_stats['total_size'])}")
        typer.echo(_rule("═"))
        typer.echo()

    executor.shutdown(wait=False)
    # A repository that could not be listed contributed no mirrors — a dry-run
    # that silently checked nothing must not look like success.
    if resolve_errors:
        raise typer.Exit(1)


def run():
    """Show what would be downloaded without fetching anything."""
    try:
        cfg = config.load()
    except FileNotFoundError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)

    asyncio.run(_check_all(cfg))
