import asyncio
import concurrent.futures
import time
from pathlib import Path

import typer

from mmw.commands.common import eprint, interruptible, load_config_or_none
from mmw.core import config
from mmw.core.downloader import DownloadResult, MirrorSummary, Status
from mmw.core.download_coordinator import DownloadCoordinator
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


async def _mirror_all(cfg: config.Config) -> int:
    """Sync every resolved mirror. Returns a process exit code."""
    mirror_dir = cfg.settings.mirror_dir

    summaries: list[MirrorSummary] = []
    # Mirrors whose sync raised. Counted, never merely printed: an exception
    # appends no summary, so before this a run where EVERY mirror raised had
    # `total_failed == 0`, `metadata_failed == []` and `resolve_errors == []`
    # at the gate and exited 0 — a nightly cron that downloaded nothing
    # reporting success, the exact mode this project documents itself against.
    mirror_errors: list[str] = []
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=cfg.settings.max_connections)
    try:
        return await _mirror_all_inner(cfg, mirror_dir, summaries, mirror_errors, executor)
    finally:
        # In a `finally`, not after the loop: an exception (or a Ctrl-C) on the
        # way out used to leave the executor's workers running, which is how a
        # stopped sync produced "Exception ignored on threading shutdown".
        executor.shutdown(wait=False)


async def _mirror_all_inner(cfg, mirror_dir, summaries, mirror_errors, executor) -> int:
    try:
        mirror_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # The single most likely real-world misconfiguration (NFS not mounted,
        # wrong uid). It already exited 1; it did so via a 121-line traceback.
        eprint(f"Cannot use mirror_dir {mirror_dir}: {e.strerror or e}")
        return 1

    keys_coord = KeySyncCoordinator(mirror_dir)

    mirrors, resolve_errors = resolve_and_report(cfg)
    from mmw.core.item_progress import ItemProgressSession
    items = ItemProgressSession(mirrors)

    try:
        pool_cm = Pool(mirror_dir)
    except OSError as e:
        eprint(f"Cannot open the pool in {mirror_dir}: {e.strerror or e}")
        return 1

    with pool_cm as pool:
        downloads = DownloadCoordinator()
        for mirror in mirrors:
            typer.echo(f"\nMirroring {mirror.name} ...")
            typer.echo("  fetching metadata...")
            t0 = time.monotonic()
            events = SyncEvents(
                started=lambda n, t0=t0: typer.echo(f"  {n:,} packages  ({time.monotonic() - t0:.1f}s)"),
                items_started=lambda inventory, t0=t0: typer.echo(
                    f"  {len(inventory.ids):,} {inventory.label}  ({time.monotonic() - t0:.1f}s)"),
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
                    items_session=items,
                    downloads=downloads,
                )
            except Exception as e:
                typer.echo(f"  ERROR: {e}", err=True)
                mirror_errors.append(f"{mirror.name}: {e}")
                continue
            summaries.append(summary)
            counts = summary.items or summary

            typer.echo(
                f"  Done — ↓{counts.downloaded} downloaded  "
                f"={counts.pooled} from pool  "
                f"✗{counts.failed} failed"
            )
            if summary.timings:
                typer.echo(f"  Timings: {summary.timing_summary()}")
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
        counts = s.items or s
        typer.echo(
            f"  {s.mirror:<35} "
            f"↓{counts.downloaded:>5}  ={counts.pooled:>5}  ✗{counts.failed:>3}{meta_flag}"
        )
        if not s.metadata_ok:
            metadata_failed.append(s.mirror)

    for counts in items.snapshot():
        typer.echo(f"  {counts.total:,} {counts.label}: ↓{counts.downloaded:,} downloaded  "
                   f"={counts.pooled:,} from pool  ✗{counts.failed:,} failed")
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
    if mirror_errors:
        typer.echo(
            f"\n  {len(mirror_errors)} mirror(s) failed outright: "
            + ", ".join(mirror_errors),
            err=True,
        )
    if total_failed or metadata_failed or resolve_errors or mirror_errors:
        if total_failed:
            typer.echo(f"\n  {total_failed} package failure(s). See output above.", err=True)
        return 1
    return 0


@interruptible
def run(cfg_dir: Path = Path(".")) -> int:
    """Sync configured mirrors to local storage. Returns a process exit code."""
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1

    return asyncio.run(_mirror_all(cfg))
