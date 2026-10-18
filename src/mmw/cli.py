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
    help="Mirror Mirror on the Wall — APT repository mirror for airgapped networks.",
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
    """Mirror Mirror on the Wall — APT repository mirror for airgapped networks.

    `--version` matters more here than for most tools: the machine that hits a
    bug is often on the far side of an airgap, with no `git log`, no network
    and no way to tell which build produced the bundle it is holding.
    """


def _load_config_or_exit(cfg_dir: Path):
    """Load mmw.toml, exiting with a readable message on any failure — the
    same door (`commands.common.load_config`) every headless verb uses."""
    from mmw.commands.common import ConfigError, load_config
    try:
        return load_config(cfg_dir)
    except ConfigError as e:
        typer.echo(str(e), err=True)
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
    max_connections: int = typer.Option(10, min=1, help="Concurrent download connections"),
    force: bool = typer.Option(False, "--force", help="Re-initialize even if config exists"),
):
    """Setup wizard. Launches manage if config already exists."""
    cfg_dir = _cfg_dir()
    cfg_path = config.config_path(cfg_dir)
    defaults = {"mirror_dir": mirror_dir, "max_connections": max_connections}

    if cfg_path.exists() and not force:
        typer.echo(f"Config found at {cfg_path} — launching manage.")
        cfg = _load_config_or_exit(cfg_dir)
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
        # What the pool holds for the configured repositories (config
        # expansion only as a fallback before the first sync): a source left
        # at "all arches" expands to nothing offline, so `cfg.mirrors` listed
        # zero mirrors for exactly the configs the README suggests checking.
        from mmw.core.resolve import available_mirrors, needs_resolving
        mirrors = available_mirrors(cfg, cfg.settings.mirror_dir)
        typer.echo(f"Mirror dir : {cfg.settings.mirror_dir}")
        typer.echo(f"Connections: {cfg.settings.max_connections}")
        typer.echo(f"Mirrors    : {len(mirrors)}")
        typer.echo("")
        for m in mirrors:
            typer.echo(f"  {m.name}  ({m.provider})")
        if not mirrors and needs_resolving(cfg.sources):
            typer.echo("  (\"all\" suites/components/arches are resolved at the first "
                       "sync — `mmw dry-run` lists what they expand to)")
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

    with _profiling(profile) as prof:
        _run_tui(_open_app(cfg, cfg_dir, "sync", start_sync_selection=(([], []), dry_run)))
    if prof is not None:
        typer.echo(f"Profile written to {prof}")


@app.command("retry-failed")
def cmd_retry_failed(
    network: str = typer.Option(None, "--network", "-n", help="Limit recovery to this network"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Count missing files without downloading"),
):
    """Retry missing VS Code files from saved metadata, without a full sync.

    Runs headless. Uses the existing snapshot and HTTP retry/backoff policy;
    does not refresh the marketplace or update metadata.
    """
    from mmw.commands.retry_failed import run
    raise typer.Exit(run(_cfg_dir(), network=network, dry_run=dry_run))


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
    snapshot: str = typer.Option(
        None, "--snapshot", "-s",
        help="Publish this snapshot (stamp, label or id — see `mmw snapshot list`) "
             "instead of the current state; 'live' is the current state. Needs -n."),
    root: Path = typer.Option(
        None, "--root",
        help="Publish into this directory instead of the network's root. Its nginx "
             "config points there with the same hostnames, so serve it on its own "
             "address. Needs -n."),
    recheck: bool = typer.Option(
        False, "--recheck",
        help="Ignore the record of the last publish and recheck every published file."),
    take_snapshot: bool = typer.Option(
        False, "--take-snapshot", help="Take a snapshot of the network first."),
):
    """Publish mirrors to network targets. Without --quiet opens the TUI
    (--snapshot / --root / --recheck / --take-snapshot always run headless)."""
    cfg_dir = _cfg_dir()
    cfg = _load_config_or_exit(cfg_dir)

    if quiet or dry_run or snapshot or root or recheck or take_snapshot:
        from mmw.commands.publish import run
        raise typer.Exit(run(cfg_dir, network=network, dry_run=dry_run,
                             allow_incomplete=allow_incomplete,
                             snapshot=snapshot, root=root, recheck=recheck,
                             take_snapshot=take_snapshot))

    _run_tui(_open_app(cfg, cfg_dir, "networks"))


@app.command("add")
def cmd_add(
    what: str = typer.Argument(
        None, help="`vscode` or `pypi` to add (or replace) that source; omit for apt files.",
        metavar="[vscode|pypi]"),
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
    arch: list[str] = typer.Option(
        None, "--arch",
        help="apt: the architecture to use when the file does not state one "
             "(default: leave unset — every architecture upstream serves). "
             "pypi: amd64 or arm64 (repeatable; default both).",
    ),
    keys_dir: Path = typer.Option(
        None, "--keys-dir",
        help="Directory of keyrings (searched recursively). A `Signed-By:` path "
             "is matched by basename; .gpg and .asc are both accepted.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the resulting [[sources]] TOML without writing.",
    ),
    kind: list[str] = typer.Option(
        None, "--kind", help="vscode: extensions, desktop, server (repeatable; default all)."),
    platform: list[str] = typer.Option(
        None, "--platform", help="vscode: target platform, e.g. linux-x64; pypi: linux-glibc, linux-musl, "
                                 "windows, macos (repeatable; default all)."),
    quality: list[str] = typer.Option(
        None, "--quality", help="vscode: stable or insider (repeatable; default both)."),
    top_n: int = typer.Option(None, "--top-n", min=1, help="vscode: the N most-installed extensions."),
    ext_id: list[str] = typer.Option(
        None, "--id", help="vscode: an extension id, publisher.name (repeatable)."),
    ids_file: Path = typer.Option(None, "--ids-file", help="vscode: a file of extension ids, one per line."),
    all_: bool = typer.Option(False, "--all", help="vscode: every extension on the marketplace; pypi: "
                                                  "every project, latest + dependencies (both very large)."),
    prerelease: bool = typer.Option(False, "--prerelease", help="vscode: allow prerelease versions."),
    python: list[str] = typer.Option(None, "--python", help="pypi: a Python version, e.g. 3.12 (repeatable; default all)."),
    glibc: str = typer.Option(None, "--glibc", help="pypi: oldest far-side glibc (default 2.31)."),
    musl: str = typer.Option(None, "--musl", help="pypi: oldest far-side musl (default 1.2)."),
    macos: str = typer.Option(None, "--macos", help="pypi: oldest far-side macOS (default 11.0)."),
    sdists: bool = typer.Option(False, "--sdists", help="pypi: also mirror every kept release's sdist."),
):
    """Add sources from a sources.list or deb822 .sources file — or the VS Code source.

    Everything is taken from the file — no discovery, no prompts:

      mmw add -f /etc/apt/sources.list.d/ubuntu.sources
      cat ubuntu.sources | mmw add
      mmw add --url https://example.com/repo.list --dry-run
      mmw add -f /etc/apt/sources.list.d --keys-dir /path/to/keyrings
      mmw add vscode --kind extensions --top-n 200 --platform linux-x64
      mmw add pypi --python 3.12 --platform linux-glibc --arch amd64 --all
    """
    if what == "pypi":
        from mmw.commands.add_pypi import run as run_pypi
        raise typer.Exit(run_pypi(
            _cfg_dir(), pythons=python, platforms=platform, arches=arch, glibc=glibc, musl=musl,
            macos=macos, sdists=sdists, all_=all_, dry_run=dry_run, echo=typer.echo))
    if what is not None:
        if what != "vscode":
            typer.echo(f"Unknown source kind {what!r} — use `mmw add vscode` or `mmw add pypi`, "
                       "or no argument for apt files.", err=True)
            raise typer.Exit(2)
        from mmw.commands.add_vscode import run as run_vscode
        raise typer.Exit(run_vscode(
            _cfg_dir(), kinds=kind, platforms=platform, qualities=quality, top_n=top_n,
            ids=ext_id, ids_file=ids_file, all_=all_, prerelease=prerelease,
            dry_run=dry_run, echo=typer.echo))
    if arch and len(arch) > 1:
        typer.echo("apt files take one --arch (the default for records that state none).", err=True)
        raise typer.Exit(2)
    from mmw.commands.add import run
    code = run(file=file, url=url, label=label, arch=arch[0] if arch else "", keys_dir=keys_dir,
               dry_run=dry_run, cfg_dir=_cfg_dir(), echo=typer.echo)
    raise typer.Exit(code)


@app.command("move-database")
def cmd_move_database(
    path: Path = typer.Argument(..., help="New path for the pool database file, e.g. on a fast disk"),
):
    """Move the pool database (settings.database) and point mmw.toml at it."""
    from mmw.commands.move_database import run
    raise typer.Exit(run(_cfg_dir(), path))


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
    out: Path = typer.Option(None, "--out", "-o",
                             help="Write here instead of the network's bundle_dir"),
    allow_incomplete: bool = typer.Option(
        False, "--allow-incomplete",
        help="Bundle even if the pool cannot serve everything the indexes promise. "
             "The gap is invisible in the bundle and surfaces as a 404 during "
             "`apt install` on the far side, so you are told exactly what is missing.",
    ),
    from_ref: str = typer.Option(
        None, "--from", help="Snapshot the far side is at (stamp, label or id), or 'none' "
                             "for a full bundle. Default: where the last bundle went."),
    to_ref: str = typer.Option(
        None, "--to", help="Snapshot to bring it to (stamp, label or id). "
                           "Default: a new snapshot of now."),
):
    """Write a network's mirrors to a transferable bundle — between any two
    snapshots (see `mmw snapshot list`). By default: from where the last
    bundle went, to a new snapshot of now."""
    from mmw.commands.bundle import run
    raise typer.Exit(run(_cfg_dir(), network=network, out=out,
                         allow_incomplete=allow_incomplete,
                         from_ref=from_ref, to_ref=to_ref))


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
                        network: str = _SNAP_NET,
                        yes: bool = typer.Option(
                            False, "--yes", "-y",
                            help="Also when it is where the last bundle went")):
    """Delete a snapshot. `mmw prune` then frees the files only it kept.
    One a directory serves is refused: publish something else there first."""
    from mmw.commands.snapshot import delete
    raise typer.Exit(delete(_cfg_dir(), network, ref, yes))


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
    stat_blobs: bool = typer.Option(False, "--stat",
                                    help="Also stat every blob (slow: one stat per file)"),
    hash_blobs: bool = typer.Option(False, "--hash",
                                    help="Also re-hash every blob (slowest: reads the whole pool)"),
    repair: bool = typer.Option(False, "--repair",
                                help="Drop damaged blobs so the next sync downloads them again "
                                     "(implies --stat)"),
):
    """Check the pool holds every blob its mirrors' refs want (read-only unless --repair)."""
    from mmw.commands.verify import run
    raise typer.Exit(run(_cfg_dir(), network=network, stat_blobs=stat_blobs,
                         hash_blobs=hash_blobs, repair=repair))
