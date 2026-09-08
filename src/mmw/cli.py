import contextlib
import signal
import sys
import termios
import time
from pathlib import Path

import typer

from mmw.core import config


def _run_tui(app):
    """Run a Textual app, restoring terminal state even if driver cleanup fails."""
    fd = sys.stdin.fileno()
    try:
        saved_tc = termios.tcgetattr(fd)
    except termios.error:
        saved_tc = None
    saved_sigwinch = signal.getsignal(signal.SIGWINCH)
    try:
        return app.run()
    finally:
        if saved_tc is not None:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, saved_tc)
            except termios.error:
                pass
        signal.signal(signal.SIGWINCH, saved_sigwinch)
        # Disable terminal modes Textual may have left enabled
        sys.stdout.write(
            "\x1b[?2004l"   # bracketed paste off
            "\x1b[?1004l"   # focus events off
            "\x1b[?1003l"   # any-event mouse tracking off
            "\x1b[?1000l"   # basic mouse tracking off
            "\x1b[?1006l"   # SGR mouse extension off
            "\x1b[?1015l"   # URXVT mouse extension off
            "\x1b[<u"       # kitty keyboard protocol pop
            "\x1b[?25h"     # show cursor
            "\x1b[?1049l"   # leave alternate screen (no-op if already normal)
        )
        sys.stdout.flush()

app = typer.Typer(
    name="mmw",
    help="Mirror Mirror on the Wall — universal package repository mirror.",
    no_args_is_help=True,
)


def _load_config_or_exit(cfg_dir: Path):
    """Load mmw.toml, exiting with a readable message on any failure."""
    try:
        return config.load(cfg_dir)
    except FileNotFoundError:
        typer.echo("No config found. Run `mmw init` first.", err=True)
        raise typer.Exit(1)
    except Exception as e:
        typer.echo(f"Invalid config at {config.config_path(cfg_dir)}:\n{e}", err=True)
        raise typer.Exit(1)


@contextlib.contextmanager
def _profiling(enabled: bool):
    """Run the block under the sampling profiler, yielding the report path.

    A TUI owns the screen, so the report goes to a file and the path is printed
    after the app tears down. Yields None when disabled, so callers need no
    second code path.
    """
    if not enabled:
        yield None
        return
    from mmw.core.profiler import SamplingProfiler

    yield_path = Path(f"mmw-profile-{time.strftime('%Y%m%d-%H%M%S')}.txt")
    # Interim reports every 10 minutes: a multi-hour sync must not lose its
    # profile to a crash, and the snapshots show the picture changing between
    # phases rather than only the average over the whole run.
    prof = SamplingProfiler(snapshot=yield_path, snapshot_every=600.0)
    prof.start()
    try:
        yield yield_path
    finally:
        prof.stop()
        try:
            prof.write(yield_path)
        except Exception as e:                       # never lose the run to this
            typer.echo(f"Could not write profile: {e}", err=True)


def _resolved_for_sync(cfg, selection):
    """Resolve "all" dimensions, then narrow to what was selected.

    The manage list hands back a `(repository identities, mirror names)` pair;
    the concrete mirror set only exists after listing upstream, so resolution
    happens here — and what "all" expanded to is printed before the sync TUI
    takes the screen.

    **Identities are what survive resolution.** A mirror name chosen before the
    first sync names the *config expansion*, which resolution may widen right
    past (`arches = []` expands to one invented `amd64` offline and to five real
    arches upstream). Selecting the repository keeps meaning the repository.
    """
    from mmw.commands.mirror import resolve_and_report

    sources, names = _split_selection(selection)
    mirrors, errors = resolve_and_report(cfg)
    if sources or names:
        chosen = [m for m in mirrors if m.source in sources or m.name in names]
        if not chosen:
            # Never widen a selection back to everything: syncing terabytes
            # nobody asked for is far worse than syncing nothing and saying so.
            typer.echo("Nothing selected could be resolved — is it still configured?",
                       err=True)
            return None
        mirrors = chosen
    if not mirrors:
        typer.echo("Nothing to sync." if not errors else "No mirrors could be resolved.",
                   err=bool(errors))
        return None
    return mirrors


def _split_selection(selection) -> tuple[set[str], set[str]]:
    """(repository identities, mirror names) from what the manage TUI returned."""
    if isinstance(selection, tuple):
        sources, names = selection
        return set(sources or ()), set(names or ())
    return set(), set(selection or ())


def _run_manage_sync_loop(cfg, cfg_dir, initial_tab: str = "tab-manage"):
    """Run ManageApp ↔ SyncApp in a loop, handling 'sync' and 'manage' results."""
    from mmw.tui.manage import ManageApp
    from mmw.tui.sync import SyncApp

    result = _run_tui(ManageApp(cfg, cfg_dir, initial_tab=initial_tab))
    while True:
        if isinstance(result, tuple) and result[0] == "sync":
            mirrors = _resolved_for_sync(cfg, result[1])
            if mirrors is None:
                return None
            result = _run_tui(SyncApp(cfg, mirrors=mirrors))
            if result == "manage":
                result = _run_tui(ManageApp(cfg, cfg_dir, initial_tab=initial_tab))
                continue
            break
        else:
            return result  # reinit or None
    return None


@app.command("init")
def cmd_init(
    mirror_dir: Path = typer.Option(Path("./mirror"), help="Mirror storage directory"),
    max_connections: int = typer.Option(10, help="Concurrent download connections"),
    force: bool = typer.Option(False, "--force", help="Re-initialize even if config exists"),
):
    """Setup wizard. Launches manage if config already exists."""
    from mmw.tui.init_wizard import InitWizardApp

    cfg_dir = Path(".")
    cfg_path = config.config_path(cfg_dir)

    if cfg_path.exists() and not force:
        typer.echo(f"Config found at {cfg_path} — launching manage.")
        try:
            cfg = config.load(cfg_dir)
        except Exception as e:
            typer.echo(f"Error loading config: {e}", err=True)
            raise typer.Exit(1)
        result = _run_manage_sync_loop(cfg, cfg_dir)
        if result == "reinit":
            _run_tui(InitWizardApp(cfg_dir, mirror_dir=mirror_dir, max_connections=max_connections))
        return

    result = _run_tui(InitWizardApp(cfg_dir, mirror_dir=mirror_dir, max_connections=max_connections))
    if result == "done":
        typer.echo(f"Config written to {cfg_path}")


@app.command("manage")
def cmd_manage(
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print mirror list to stdout, no TUI"),
):
    """Manage configured mirrors (add, remove)."""
    from mmw.tui.init_wizard import InitWizardApp

    cfg_dir = Path(".")
    cfg = _load_config_or_exit(cfg_dir)

    if quiet:
        typer.echo(f"Mirror dir : {cfg.settings.mirror_dir}")
        typer.echo(f"Connections: {cfg.settings.max_connections}")
        typer.echo(f"Mirrors    : {len(cfg.mirrors)}")
        typer.echo("")
        for m in cfg.mirrors:
            typer.echo(f"  {m.name}  ({m.provider})")
        return

    result = _run_manage_sync_loop(cfg, cfg_dir)
    if result == "reinit":
        _run_tui(InitWizardApp(cfg_dir))


@app.command("sync")
def cmd_sync(
    quiet: bool = typer.Option(False, "--quiet", "-q", help="No TUI — plain output for cron/headless use"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be downloaded without fetching"),
    profile: bool = typer.Option(
        False, "--profile",
        help="Sample every thread's stack and CPU use; write a report on exit. "
             "In-process, so it needs no ptrace/root.",
    ),
):
    """Sync configured mirrors. Use --quiet for cron jobs."""
    cfg_dir = Path(".")
    cfg = _load_config_or_exit(cfg_dir)

    if dry_run and quiet:
        from mmw.commands.dry_run import run
        run()
        return

    if quiet:
        from mmw.commands.mirror import run
        run()
        return

    # "all" dimensions need the network to resolve, and the sync TUI builds its
    # cards in compose() — so resolve before the app starts, echoing what a
    # blank suites/components/arches list expanded to.
    from mmw.commands.mirror import resolve_and_report
    mirrors, errors = resolve_and_report(cfg)
    if not mirrors:
        typer.echo("Nothing to sync." if not errors else "No mirrors could be resolved.", err=bool(errors))
        raise typer.Exit(1 if errors else 0)

    from mmw.tui.sync import SyncApp
    with _profiling(profile) as prof:
        result = _run_tui(SyncApp(cfg, mirrors=mirrors, dry_run=dry_run))
    if prof is not None:
        typer.echo(f"Profile written to {prof}")
    if result == "manage":
        _run_manage_sync_loop(cfg, cfg_dir)


@app.command("dry-run")
def cmd_dry_run():
    """Show what would be downloaded without fetching anything."""
    from mmw.commands.dry_run import run
    run()


@app.command("publish")
def cmd_publish(
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Headless publish — no TUI"),
    network: str = typer.Option(None, "--network", "-n", help="Publish a specific network by name"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be linked without writing"),
):
    """Publish mirrors to network targets. Without --quiet opens the TUI."""
    cfg_dir = Path(".")
    cfg = _load_config_or_exit(cfg_dir)

    if quiet or dry_run:
        from mmw.core.publisher import dry_run_network, publish_network

        networks = cfg.networks
        if not networks:
            typer.echo("No networks configured. Use `mmw publish` (TUI) to add one.")
            raise typer.Exit(1)

        if network:
            networks = [n for n in networks if n.name == network]
            if not networks:
                typer.echo(f"Network '{network}' not found.", err=True)
                raise typer.Exit(1)

        label = "[dry-run] " if dry_run else ""
        for net in networks:
            typer.echo(f"\n{label}Publishing network: {net.name}  →  {net.root}")
            if dry_run:
                result = dry_run_network(net, cfg, cfg.settings.mirror_dir)
            else:
                result = publish_network(net, cfg, cfg.settings.mirror_dir)
            for mr in result.mirrors:
                status = f"  {mr.mirror_name:<40} linked={mr.linked}  skipped={mr.skipped}"
                if mr.errors:
                    status += f"  errors={len(mr.errors)}"
                typer.echo(status)
                for e in mr.errors:
                    typer.echo(f"    ✗ {e}", err=True)
            typer.echo(
                f"\n  Total: {result.linked} {'would link' if dry_run else 'linked'}, "
                f"{result.skipped} already current"
                + (f", {len(result.errors)} error(s)" if result.errors else "")
            )
        return

    from mmw.tui.init_wizard import InitWizardApp

    result = _run_manage_sync_loop(cfg, cfg_dir, initial_tab="tab-networks")
    if result == "reinit":
        _run_tui(InitWizardApp(cfg_dir))


@app.command("add")
def cmd_add(
    file: list[Path] = typer.Option(
        None, "--file", "-f",
        help="apt config file(s) or a directory of them ('-' for stdin). "
             "Repeatable; joined with blank lines so records never run together. "
             "Defaults to stdin.",
    ),
    url: str = typer.Option(None, "--url", help="Fetch the config file from a URL."),
    label: str = typer.Option(
        None, "--label",
        help="Override the key-grouping label (default: the host's domain name).",
    ),
    arch: str = typer.Option(
        "", "--arch",
        help="Architecture to use when the file does not state one "
             "(default: leave unset — every architecture upstream serves).",
    ),
    keys_dir: Path = typer.Option(
        None, "--keys-dir",
        help="Directory of keyrings (searched recursively). A `Signed-By:` path "
             "is matched by basename; .gpg and .asc are both accepted.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the resulting [[sources]] TOML without writing.",
    ),
):
    """Add sources from a sources.list or deb822 .sources file.

    Everything is taken from the file — no discovery, no prompts:

      mmw add -f /etc/apt/sources.list.d/ubuntu.sources
      cat ubuntu.sources | mmw add
      mmw add --url https://example.com/repo.list --dry-run
      mmw add -f /etc/apt/sources.list.d --keys-dir /path/to/keyrings
    """
    from mmw.commands.add import run
    code = run(file=file, url=url, label=label, arch=arch, keys_dir=keys_dir,
               dry_run=dry_run, echo=typer.echo)
    raise typer.Exit(code)


@app.command("prune")
def cmd_prune():
    """Remove orphaned pool packages not referenced by any mirror."""
    from mmw.commands.prune import run
    run()
