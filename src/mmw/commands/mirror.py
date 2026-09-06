import asyncio
import concurrent.futures
import time

import typer

from mmw.core import config
from mmw.core.downloader import DownloadResult, MirrorSummary, Status
from mmw.core.format import fmt_size as _fmt_size
from mmw.core.pool import Pool
from mmw.core.resolve import resolve_sources
from mmw.core.sync_engine import KeySyncCoordinator, SyncEvents, sync_mirror


def _make_progress(mirror_name: str):
    icons = {
        Status.DOWNLOADED: "↓",
        Status.POOLED:     "=",
        Status.SKIPPED:    " ",
        Status.FAILED:     "✗",
    }

    def callback(pkg_name: str, result: DownloadResult):
        icon = icons[result.status]
        if result.status == Status.FAILED:
            typer.echo(f"  [{icon}] {pkg_name}: {result.error}")
        elif result.status == Status.DOWNLOADED:
            typer.echo(f"  [{icon}] {pkg_name} ({_fmt_size(result.bytes_transferred)})")

    return callback


def resolve_and_report(cfg: config.Config, echo=typer.echo):
    """Expand any "all" dimensions, telling the user what they expanded to.

    A blank suites/components/arches list means "everything upstream serves";
    it must never quietly become a hundred mirrors, so what it resolved to is
    printed before anything is fetched.
    """
    mirrors, notes, errors = resolve_sources(cfg.sources, progress=None)
    for note in notes:
        echo(f"  {note}")
    for err in errors:
        echo(f"  ⚠  {err}")
    return mirrors, errors


async def _mirror_all(cfg: config.Config):
    mirror_dir = cfg.settings.mirror_dir
    mirror_dir.mkdir(parents=True, exist_ok=True)

    summaries: list[MirrorSummary] = []
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=cfg.settings.max_connections)
    keys_coord = KeySyncCoordinator(mirror_dir)

    mirrors, resolve_errors = resolve_and_report(cfg)

    with Pool(mirror_dir) as pool:
        for mirror in mirrors:
            typer.echo(f"\nMirroring {mirror.name} ...")
            typer.echo("  fetching metadata...")
            t0 = time.monotonic()
            events = SyncEvents(
                started=lambda n, t0=t0: typer.echo(f"  {n:,} packages  ({time.monotonic() - t0:.1f}s)"),
                progress=_make_progress(mirror.name),
            )
            try:
                summary = await sync_mirror(
                    mirror,
                    pool=pool,
                    mirror_dir=mirror_dir,
                    max_connections=cfg.settings.max_connections,
                    executor=executor,
                    events=events,
                    keys=keys_coord,
                )
            except Exception as e:
                typer.echo(f"  ERROR: {e}", err=True)
                continue
            summaries.append(summary)

            typer.echo(
                f"  Done — ↓{summary.downloaded} downloaded  "
                f"={summary.pooled} from pool  "
                f"✗{summary.failed} failed"
            )
            if summary.metadata_missing:
                typer.echo(f"  note: {summary.metadata_missing} index-listed metadata file(s) absent upstream (404)")
            if summary.metadata_resnapshotted and summary.metadata_ok:
                typer.echo("  note: upstream republished during sync — re-read the index and used the newer one")
            for err in summary.metadata_errors:
                typer.echo(f"  ⚠ metadata: {err}", err=True)
            if not summary.metadata_ok:
                typer.echo("  ⚠ metadata NOT updated — mirror serves the previous index", err=True)
            for err in summary.key_errors:
                typer.echo(f"  ⚠ GPG key: {err}", err=True)

    executor.shutdown(wait=False)

    if keys_coord.results:
        typer.echo("\nGPG keys:")
        for name, kr in sorted(keys_coord.results.items()):
            typer.echo(f"  {name}: {kr.fetched} fetched, {kr.skipped} already present → {kr.dest_dir}")

    typer.echo("\n" + "─" * 50)
    typer.echo("Summary")
    typer.echo("─" * 50)
    total_bytes = total_failed = total_key_errors = 0
    metadata_failed: list[str] = []
    for s in summaries:
        total_bytes += s.bytes_transferred
        total_failed += s.failed
        total_key_errors += len(s.key_errors)
        meta_flag = "" if s.metadata_ok else "  ⚠ metadata stale"
        typer.echo(
            f"  {s.mirror:<35} "
            f"↓{s.downloaded:>5}  ={s.pooled:>5}  ✗{s.failed:>3}{meta_flag}"
        )
        if not s.metadata_ok:
            metadata_failed.append(s.mirror)

    typer.echo(f"\n  Transferred: {_fmt_size(total_bytes)}")

    if total_key_errors:
        typer.echo(
            f"\n  ⚠ {total_key_errors} GPG key fetch failure(s) — airgapped clients may be unable to verify.",
            err=True,
        )
    if metadata_failed:
        typer.echo(
            f"\n  Metadata NOT updated for {len(metadata_failed)} mirror(s): "
            + ", ".join(metadata_failed),
            err=True,
        )
    if resolve_errors:
        typer.echo(
            f"\n  {len(resolve_errors)} repositor(y/ies) could not be listed — "
            "nothing was synced for them.",
            err=True,
        )
    if total_failed or metadata_failed or resolve_errors:
        if total_failed:
            typer.echo(f"\n  {total_failed} package failure(s). See output above.", err=True)
        raise typer.Exit(1)


def run():
    """Sync configured mirrors to local storage."""
    try:
        cfg = config.load()
    except FileNotFoundError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)

    asyncio.run(_mirror_all(cfg))
