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


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("mirror-mirror-on-the-wall")
    except Exception:
        return "unknown (not installed as a package)"


# Where mmw.toml lives for this invocation. A module global rather than a
# parameter threaded through ten commands: typer callbacks run before the
# command body, so this is set by then, and the alternative is nine copies of
# `Path(".")` — which is what was here, and is why `--config` could not exist.
_CFG_DIR = Path(".")


def _cfg_dir() -> Path:
    return _CFG_DIR


def _config_callback(value: "Path | None") -> "Path | None":
    global _CFG_DIR
    if value is not None:
        _CFG_DIR = value
    return value


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"mmw {_version()}")
        raise typer.Exit(0)


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_callback, is_eager=True,
        help="Show the version and exit.",
    ),
    config_dir: Path = typer.Option(
        None, "--config", "-c", callback=_config_callback, is_eager=True,
        metavar="DIR",
        help="Directory holding mmw.toml (default: the working directory).",
    ),
) -> None:
    """Mirror Mirror on the Wall — universal package repository mirror.

    `--version` matters more here than for most tools: the machine that hits a
    bug is often on the far side of an airgap, with no `git log`, no network
    and no way to tell which build produced the bundle it is holding.
    """


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


def _open_app(cfg, cfg_dir, section: str = "repositories", **kwargs):
    """Build MmwApp — the one TUI — at `section`. With no config it opens on Setup."""
    from mmw.tui.app import MmwApp
    return MmwApp(cfg, cfg_dir, initial_section=section, **kwargs)


@app.command("init")
def cmd_init(
    mirror_dir: Path = typer.Option(Path("./mirror"), help="Mirror storage directory"),
    max_connections: int = typer.Option(10, help="Concurrent download connections"),
    force: bool = typer.Option(False, "--force", help="Re-initialize even if config exists"),
):
    """Setup wizard. Launches manage if config already exists."""
    cfg_dir = _cfg_dir()
    cfg_path = config.config_path(cfg_dir)
    defaults = {"mirror_dir": mirror_dir, "max_connections": max_connections}

    if cfg_path.exists() and not force:
        typer.echo(f"Config found at {cfg_path} — launching manage.")
        try:
            cfg = config.load(cfg_dir)
        except Exception as e:
            typer.echo(f"Error loading config: {e}", err=True)
            raise typer.Exit(1)
        _run_tui(_open_app(cfg, cfg_dir, **defaults))
        return

    # --force keeps the old file until Setup writes the new one.
    _run_tui(_open_app(None, cfg_dir, "setup", **defaults))


@app.command("manage")
def cmd_manage(
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Print mirror list to stdout, no TUI"),
    profile: bool = typer.Option(
        False, "--profile",
        help="Sample every thread's stack and CPU use; write a report on exit. "
             "In-process, so it needs no ptrace/root. Covers the whole session "
             "(publish, bundle, sync-from-manage), not just one action.",
    ),
):
    """Manage configured mirrors (add, remove)."""
    cfg_dir = _cfg_dir()
    cfg = _load_config_or_exit(cfg_dir)

    if quiet:
        typer.echo(f"Mirror dir : {cfg.settings.mirror_dir}")
        typer.echo(f"Connections: {cfg.settings.max_connections}")
        typer.echo(f"Mirrors    : {len(cfg.mirrors)}")
        typer.echo("")
        for m in cfg.mirrors:
            typer.echo(f"  {m.name}  ({m.provider})")
        return

    with _profiling(profile) as prof:
        _run_tui(_open_app(cfg, cfg_dir))
    if prof is not None:
        typer.echo(f"Profile written to {prof}")


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
    cfg_dir = _cfg_dir()
    cfg = _load_config_or_exit(cfg_dir)

    # `raise typer.Exit(run(...))`, not a bare `run()`: both return a process
    # exit code and dropping it made `mmw sync --quiet` exit 0 no matter what
    # happened — the exact silent-cron failure the exit codes exist for.
    #
    # Wrapped in `_profiling` too. Both quiet branches used to `return` before
    # reaching it, so `--profile` was a silent no-op on precisely the run worth
    # profiling: a headless multi-hour cron sync. (The profiler exists because
    # ptrace is restricted on this machine, so there is no outside-in option.)
    if dry_run and quiet:
        from mmw.commands.dry_run import run
        with _profiling(profile) as prof:
            code = run(cfg_dir)
        if prof is not None:
            typer.echo(f"Profile written to {prof}")
        raise typer.Exit(code)

    if quiet:
        from mmw.commands.mirror import run
        with _profiling(profile) as prof:
            code = run(cfg_dir)
        if prof is not None:
            typer.echo(f"Profile written to {prof}")
        raise typer.Exit(code)

    # "all" dimensions need the network to resolve, and the sync TUI builds its
    # cards in compose() — so resolve before the app starts, echoing what a
    # blank suites/components/arches list expanded to.
    from mmw.commands.mirror import resolve_and_report
    mirrors, errors = resolve_and_report(cfg)
    if not mirrors:
        typer.echo("Nothing to sync." if not errors else "No mirrors could be resolved.", err=bool(errors))
        raise typer.Exit(1 if errors else 0)

    with _profiling(profile) as prof:
        _run_tui(_open_app(cfg, cfg_dir, "sync", start_sync_with=(mirrors, dry_run)))
    if prof is not None:
        typer.echo(f"Profile written to {prof}")


@app.command("dry-run")
def cmd_dry_run():
    """Show what would be downloaded without fetching anything."""
    from mmw.commands.dry_run import run
    raise typer.Exit(run(_cfg_dir()))


@app.command("publish")
def cmd_publish(
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Headless publish — no TUI"),
    network: str = typer.Option(None, "--network", "-n", help="Publish a specific network by name"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be linked without writing"),
    allow_incomplete: bool = typer.Option(
        False, "--allow-incomplete",
        help="Publish even if the pool cannot serve everything the indexes promise "
             "(you are still told what is missing).",
    ),
):
    """Publish mirrors to network targets. Without --quiet opens the TUI."""
    cfg_dir = _cfg_dir()
    cfg = _load_config_or_exit(cfg_dir)

    if quiet or dry_run:
        from mmw.commands.publish import run
        raise typer.Exit(run(cfg_dir, network=network, dry_run=dry_run,
                             allow_incomplete=allow_incomplete))

    _run_tui(_open_app(cfg, cfg_dir, "networks"))


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
def cmd_prune(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would be removed without deleting anything"),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the confirmation prompt (implied when not a TTY)"),
):
    """Remove orphaned pool packages not referenced by any mirror."""
    from mmw.commands.prune import run
    raise typer.Exit(run(_cfg_dir(), dry_run=dry_run, yes=yes))


@app.command("certs")
def cmd_certs(
    network: str = typer.Option(None, "--network", "-n", help="Generate certs for a specific network by name"),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Report what would be generated without writing any key material.",
    ),
    ca: Path = typer.Option(
        None, "--ca",
        help="CA certificate to sign with (PEM). With --ca-key, writes mirror.crt; "
             "the next publish/bundle then serves HTTPS.",
    ),
    ca_key: Path = typer.Option(
        None, "--ca-key",
        help="The CA's private key (PEM). Prompted for its passphrase if encrypted, "
             "or set MMW_CA_PASSPHRASE.",
    ),
    days: int = typer.Option(730, "--days", min=1, help="Validity of the signed certificate."),
):
    """Generate (or refresh) TLS key/CSR material for `certs = true` networks,
    and sign it with your CA.

      mmw certs                                  # key + CSR, and check any signed cert
      mmw certs -n lab --ca ca.crt --ca-key ca.key
    """
    from mmw.commands.certs import run
    raise typer.Exit(run(_cfg_dir(), network=network, dry_run=dry_run,
                         ca=ca, ca_key=ca_key, days=days))


@app.command("bundle")
def cmd_bundle(
    network: str = typer.Option(None, "--network", "-n", help="Bundle one network by name"),
    full: bool = typer.Option(False, "--full", help="Send the whole network, not just the delta"),
    out: Path = typer.Option(None, "--out", "-o",
                             help="Write here instead of the network's bundle_dir"),
    allow_incomplete: bool = typer.Option(
        False, "--allow-incomplete",
        help="Bundle even if the pool cannot serve everything the indexes promise. "
             "The gap is invisible in the bundle and surfaces as a 404 during "
             "`apt install` on the far side, so you are told exactly what is missing.",
    ),
):
    """Write a network's mirrors to a transferable bundle."""
    from mmw.commands.bundle import run
    raise typer.Exit(run(_cfg_dir(), network=network, full=full, out=out,
                         allow_incomplete=allow_incomplete))


snapshot_app = typer.Typer(
    help="Network snapshots: a network's exact state, kept to bundle to or publish again.",
    no_args_is_help=True,
)
app.add_typer(snapshot_app, name="snapshot")
_SNAP_NET = typer.Option(..., "--network", "-n", help="The network")


@snapshot_app.command("take")
def cmd_snapshot_take(network: str = _SNAP_NET,
                      label: str = typer.Option(None, "--label", "-l",
                                                help="A name for it, e.g. pre-upgrade")):
    """Record the network's current state as a snapshot."""
    from mmw.commands.snapshot import take
    raise typer.Exit(take(_cfg_dir(), network, label))


@snapshot_app.command("list")
def cmd_snapshot_list(network: str = _SNAP_NET):
    """List the network's snapshots, newest first, with the disk each alone holds."""
    from mmw.commands.snapshot import list_
    raise typer.Exit(list_(_cfg_dir(), network))


@snapshot_app.command("rename")
def cmd_snapshot_rename(ref: str = typer.Argument(..., help="Stamp, label or id"),
                        label: str = typer.Argument(..., help="The new label"),
                        network: str = _SNAP_NET):
    """Label (or relabel) a snapshot."""
    from mmw.commands.snapshot import rename
    raise typer.Exit(rename(_cfg_dir(), network, ref, label))


@snapshot_app.command("delete")
def cmd_snapshot_delete(ref: str = typer.Argument(..., help="Stamp, label or id"),
                        network: str = _SNAP_NET):
    """Delete a snapshot. `mmw prune` then frees the files only it kept."""
    from mmw.commands.snapshot import delete
    raise typer.Exit(delete(_cfg_dir(), network, ref))


@snapshot_app.command("clean")
def cmd_snapshot_clean(network: str = _SNAP_NET,
                       keep: int = typer.Option(..., "--keep", min=0,
                                                help="How many of the newest to keep"),
                       yes: bool = typer.Option(False, "--yes", "-y",
                                                help="Delete — without it, only preview")):
    """Delete all but the newest N snapshots (previews unless --yes)."""
    from mmw.commands.snapshot import clean
    raise typer.Exit(clean(_cfg_dir(), network, keep, yes))


@app.command("verify")
def cmd_verify(
    network: str = typer.Option(None, "--network", "-n", help="Verify only this network's mirrors"),
):
    """Check the pool holds every blob its mirrors' refs want (read-only)."""
    from mmw.commands.verify import run
    raise typer.Exit(run(_cfg_dir(), network=network))
