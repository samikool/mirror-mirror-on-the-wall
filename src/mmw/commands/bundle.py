"""Headless bundle — `mmw bundle`, the cron-safe door onto the same pipeline
the Networks tab's Bundle button drives.

Bundling is the one publish-shaped workflow that most wants a cron job: an
airgapped site's operator wants a scheduled `.tar` drop, not a TUI they
have to sit in front of. This module is a thin frontend over `bundle_network`
— and deliberately the same shape, check for check, as `commands/publish.py`:
the two verbs' only real difference is the sink.
"""
import os
import sys
from pathlib import Path

from mmw.core import disk_io
from mmw.commands.common import eprint, interruptible, load_config_or_none, preflight_ok
from mmw.core.format import fmt_size as _fmt_size

def bundle_network(*args, **kwargs):
    """Resolve the builder at call time for both frontends."""
    from mmw.core.publisher.bundle import bundle_network as build
    return build(*args, **kwargs)


def stage_network(*args, **kwargs):
    from mmw.core.publisher.bundle import stage_network as build
    return build(*args, **kwargs)


@interruptible
def run(cfg_dir: Path = Path("."), network: str | None = None,
        out: Path | None = None,
        allow_incomplete: bool = False, from_ref: str | None = None,
        to_ref: str | None = None, repos: list[str] | None = None, stage: bool = False,
        installer: bool = False, runtime_wheel: Path | None = None, history: bool | None = None,
        full: bool = False, since: str | None = None, kept: list[Path] | None = None, until: str | None = None, dry_run: bool = False) -> int:
    """Bundle one or all configured networks. Returns a process exit code.

    `from_ref` / `to_ref` name snapshots (stamp, label or id) of the one
    network `-n` names; `from_ref="none"` is a full bundle. Unset: from where
    the last bundle listed each repository, to its latest recorded points. `installer` writes mmw's
    installer beside an mmw network's bundle; `runtime_wheel` is the mmw
    wheel it ships."""
    if from_ref is not None:
        eprint("--from is deprecated — use --since DATE (a snapshot's date) or --full")
    if since is not None and (full or from_ref is not None):
        eprint("--since cannot be combined with --full or --from")
        return 1
    if until is not None and to_ref is not None:
        eprint("--until cannot be combined with --to")
        return 1
    if dry_run and (to_ref is not None or from_ref not in (None, "none")):
        eprint("--dry-run previews points; use --since / --until instead of snapshot --from / --to")
        return 1
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1

    networks = cfg.networks
    if not networks:
        eprint("No networks configured. Use `mmw manage` to add one.")
        return 1

    if network:
        networks = [n for n in networks if n.name == network]
        if not networks:
            eprint(f"Network '{network}' not found.")
            return 1

    from mmw.core.publisher.bundle import is_stdout
    streaming = is_stdout(out)
    if streaming and not network:
        eprint("--out - streams one network's bundle to stdout — pass -n too.")
        return 1
    # On a stream, stdout is the tar: every other line goes to stderr.
    say = (lambda *a: print(*a, file=sys.stderr)) if streaming else print

    if repos and not network:
        eprint("--repo names repositories of one network — pass -n too.")
        return 1

    if (from_ref or to_ref) and not network:
        eprint("--from / --to name a snapshot of one network — pass -n too.")
        return 1
    from mmw.core.publisher.bundle import _LAST
    start = None if from_ref == "none" or full else (from_ref or _LAST)
    mirror_dir = cfg.settings.mirror_dir
    ok = True
    installers_written: set[tuple] = set()      # (directory, arches, python): one installer each
    for net in networks:
        # A bundle destination is `out` (this run) or `net.bundle_dir` (the
        # config). Naming the network with `-n` and finding neither is an
        # error — you asked for something this network cannot do. Meeting it
        # while iterating every network is not: a publish-only network in a
        # bundle run is a normal config, and a multi-network cron run must
        # keep bundling the ones that ARE configured. `mmw publish` applies
        # the identical rule to `root`; the two used to disagree, each
        # claiming parity with the other.
        if not dry_run and out is None and net.bundle_dir is None:
            msg = (f"Network '{net.name}' has no bundle destination set "
                   f"(set bundle_dir in mmw.toml or pass --out)")
            if network:
                eprint(msg)
                return 1
            say(f"Skipping network: {net.name} — no bundle destination set "
                  f"(set bundle_dir in mmw.toml or pass --out)")
            continue

        keys = None
        if repos:
            keys = _repo_keys(net, cfg, mirror_dir, repos)
            if keys is None:
                return 1

        if dry_run:
            try:
                plan = preview_transfer(net, mirror_dir, since=since, until=until,
                                        full=full or from_ref == "none", history=history,
                                        repos=keys, kept_dirs=kept)
                from mmw.core.repos import repository_label
                from mmw.site.versionref import day
                say(f"{net.name}: {plan.source}")
                for row in plan.rows:
                    say(f"  {repository_label(row.repo, cfg)} / {row.suite}: "
                        f"{day(row.from_date) if row.from_date else 'full'} → {day(row.to_date)} "
                        f"· {row.points} point(s)"
                        + (f"; kept {day(row.available[0])} to {day(row.available[-1])}" if row.available else ""))
                say(f"  Sends about {plan.blobs:,} new blobs · {_fmt_size(plan.bytes)}")
                for warning in plan.warnings:
                    say(f"  {warning}")
            except Exception as exc:
                eprint(f"Network '{net.name}' failed: {exc}")
                ok = False
            continue

        # The same completeness gate `do_bundle` runs in the TUI, which the
        # headless door never had: a ref whose blob the pool lacks is
        # invisible to `pool.mirror_files()`, so it lands in neither the tar
        # nor the manifest, the far side applies it, and the gap surfaces
        # as a 404 during `apt install` on the far side of an airgap.
        # Only "now" is checked here: an existing snapshot's own files are
        # checked by the bundle itself (refused if any left the pool).
        if not to_ref and until is None and not preflight_ok("bundle", net, cfg, mirror_dir, allow_incomplete,
                                              stdout_is_payload=streaming):
            ok = False
            continue

        try:
            picked = {"repos": keys} if keys else {}
            if installer:
                where = (str(out or net.bundle_dir), tuple(sorted(net.runtime_arches)), net.runtime_python)
                if where not in installers_written:
                    picked["installer"] = True
                    installers_written.add(where)
            elif not streaming:
                from mmw.core.runtime import installer_due
                if installer_due(net, mirror_dir):
                    say(f"  {net.name}: its far side hasn't had this mmw yet — add --installer to write "
                        f"mmw's installer beside the bundle (the far side applies bundles with mmw)")
            if runtime_wheel is not None:
                picked["runtime_wheel"] = runtime_wheel
            if history is not None:
                picked["history"] = history
            # A warning the run raises before its long part (a stage that will
            # copy, not hardlink) is said then, while there is time to stop it.
            told: list[str] = []

            def progress(_rel, status, told=told):
                if status.startswith("warning:"):
                    told.append(status.split(":", 1)[1])
                    eprint(f"    ⚠ {told[-1]}")
                elif status.startswith("runtime:") and status != "runtime:done":
                    eprint(f"  mmw installer: {status.split(':', 1)[1]}")
            # A stage warns before its long part; the runtime build takes
            # minutes the first time — both are said as they happen.
            picked["progress"] = progress
            if until is not None:
                picked["until"] = until
            if since is not None:
                picked["since"] = since
                picked["kept_dirs"] = kept
            if stage:
                result = transfer(net, cfg, mirror_dir, out=out, stage=True,
                                  from_ref=start, to_ref=to_ref or "now", **picked)
            else:
                result = transfer(net, cfg, mirror_dir, out=out,
                                  from_ref=start, to_ref=to_ref or "now", **picked)
        except BrokenPipeError:
            _quiet_stdout()
            eprint("stream to stdout broke — nothing recorded; run the bundle again")
            return 1
        except Exception as e:
            eprint(f"Network '{net.name}' failed: {e}")
            ok = False
            continue

        span = (f"{result.previous} → {result.created}" if result.previous
                else f"full → {result.created}")
        say(f"{net.name}  {span}  →  {'stdout' if streaming else result.out_path}")
        if result.since_source:
            say(f"  since {result.since_source}")
        say(f"  sent={result.sent} ({_fmt_size(result.sent_bytes)})  deleted={result.deleted}")
        for r in result.repos:
            say(f"  {r.label}  " + ", ".join(f"{suite} {before or 'new'} → {to}" for suite, before, to in r.suites))
        for path in result.installers:
            say(f"  mmw installer: {path}")
        if result.timings:
            say(f"    {result.timing_summary()}")
        # The checksum sidecar, when the pipeline produces one. `getattr`
        # rather than an attribute access: `BundleResult` gained this field
        # after this command existed, and a bundle written by an older
        # pipeline has no sidecar to name.
        sha_path = getattr(result, "checksum_path", None)
        if sha_path:
            say(f"  sha256  {sha_path}")
        if result.stream_sha256:
            say(f"sha256 {result.stream_sha256}")
        # Reported, never dropped: the TUI surfaces these on the Bundle
        # result notification, and a dist-upgrade warning that is visible to
        # someone watching a modal but silent in cron is exactly the kind of
        # frontend divergence this command exists to close. They are warnings,
        # not failures, so they do not set `ok` — `mmw publish` treats the
        # same list the same way.
        for w in [*result.dist_upgrade_warnings, *result.warnings]:
            if w not in told:
                eprint(f"    ⚠ {w}")
        if result.errors:
            ok = False
            for e in result.errors:
                eprint(f"    ✗ {e}")

    return 0 if ok else 1


def _repo_keys(net, cfg, mirror_dir, refs: list[str]) -> list[str] | None:
    """The repository keys `--repo` names on `net`, or None after saying why not."""
    from mmw.core.repos import network_repos, resolve_repo_refs
    try:
        return resolve_repo_refs(refs, network_repos(net, cfg, mirror_dir))
    except (ValueError, LookupError) as e:
        eprint(str(e).strip("'\""))
        return None


def _quiet_stdout() -> None:
    """After a broken pipe: point stdout at /dev/null so the interpreter's
    final flush can't raise a second error on the way out."""
    try:
        devnull = disk_io.ops.open_fd(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except OSError:
        pass


def transfer(net, cfg, mirror_dir, *, out=None, stage=False, **options):
    """The shared command function for CLI and TUI transfers."""
    if stage:
        return stage_network(net, cfg, mirror_dir, out_dir=out, **options)
    return bundle_network(net, cfg, mirror_dir, out_path=out, **options)


def preview_since(net, mirror_dir, since, kept=None):
    """The same baseline resolver as a transfer, without writing anything."""
    from mmw.core.publisher.bundle import resolve_since
    return resolve_since(net, mirror_dir, since, kept)


def preview_transfer(net, mirror_dir, **options):
    """Read-only range planning, shared by the TUI and CLI."""
    from mmw.core.transfer_ranges import preview_transfer as preview
    return preview(net, mirror_dir, **options)
