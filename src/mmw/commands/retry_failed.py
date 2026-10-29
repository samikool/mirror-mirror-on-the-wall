"""Targeted recovery from the locally saved VS Code snapshot."""
import asyncio
import collections
import concurrent.futures
from pathlib import Path

import typer

from mmw.commands.common import eprint, interruptible, load_config_or_none
from mmw.commands.mirror import _make_progress
from mmw.core.downloader import Status, failure_reason, reason_summary
from mmw.core.pool import Pool
from mmw.core.resolve import network_mirrors, pooled_mirrors
from mmw.core.sync_engine import SyncEvents, retry_failed_mirror
from mmw.providers import get_provider, retry_scope


async def _retry(cfg, network: str | None, dry_run: bool) -> int:
    mirror_dir = cfg.settings.mirror_dir
    if network is not None:
        net = next((n for n in cfg.networks if n.name == network), None)
        if net is None:
            eprint(f"No network named {network!r}.")
            return 1
        mirrors = network_mirrors(net, cfg, mirror_dir)
    else:
        mirrors = pooled_mirrors(cfg, mirror_dir)
    skipped = [m for m in mirrors if not get_provider(m.provider).retries_failures]
    mirrors = [m for m in mirrors if get_provider(m.provider).retries_failures]
    if skipped:
        typer.echo(retry_scope(skipped))
    if not mirrors:
        eprint("Nothing to retry: no previously synced mirror of a kind that keeps a retry "
               "list. Run a normal sync first.")
        return 1
    failed = False
    with Pool(mirror_dir, read_only=dry_run) as pool, concurrent.futures.ThreadPoolExecutor(
            max_workers=cfg.settings.max_connections) as executor:
        for mirror in mirrors:
            typer.echo(f"\nRetrying missing files: {mirror.name}")
            reasons: collections.Counter[str] = collections.Counter()
            printed = _make_progress(mirror.name)

            def progress(name, result, printed=printed, reasons=reasons):
                if result.status == Status.FAILED:
                    reasons[failure_reason(result.error)] += 1
                printed(name, result)
            try:
                result = await retry_failed_mirror(
                    mirror, pool=pool, max_connections=cfg.settings.max_connections,
                    executor=executor, dry_run=dry_run,
                    events=SyncEvents(
                        phase=lambda text: typer.echo(f"  {text}"),
                        started=lambda n: typer.echo(f"  {n:,} missing file(s)"),
                        progress=progress,
                    ),
                )
                failed |= not result.ok
                typer.echo(f"  {result.total:,} missing; {result.downloaded:,} downloaded, "
                           f"{result.pooled + result.skipped:,} recovered from pool, "
                           f"{result.failed:,} failed" + (" (dry run)" if dry_run else ""))
                if reasons:
                    typer.echo(f"  Why: {reason_summary(reasons)}")
                for error in result.pool_write_errors:
                    eprint(f"  Pool write: {error}")
            except Exception as exc:
                eprint(f"  {mirror.name}: {exc}")
                failed = True
    return int(failed)


@interruptible
def run(cfg_dir: Path = Path("."), *, network: str | None = None,
        dry_run: bool = False) -> int:
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1
    try:
        return asyncio.run(_retry(cfg, network, dry_run))
    except OSError as exc:
        eprint(f"Cannot repair the pool: {exc}")
        return 1
