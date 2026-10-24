"""The three things every headless command needs, in one place.

`commands/` is the documented headless-frontend layer, but each verb grew its
own answer to the same three questions and they disagreed:

- **Loading the config.** Five commands caught only `FileNotFoundError` from
  `config.load`, so a one-line typo in `mmw.toml` — the *documented*
  `extra="forbid"` behaviour — escaped as a 50-80 line rich traceback.
  `cli._load_config_or_exit` already formatted it correctly, but only three
  commands routed through it. `load_config()` is now the single door, and
  `cli` delegates to it too.
- **Refusing an incomplete pool.** The TUI runs `verify_mirror` over a
  network's mirrors before it will publish or bundle; the headless verbs
  called `publish_network`/`bundle_network` straight out. `pool.mirror_files()`
  is `refs ⋈ packages`, so a ref whose blob the pool doesn't hold is
  *invisible* to it: it lands in neither tree nor tarball, `apply.py --verify`
  passes, and the gap surfaces as a 404 during `apt install` on the far side
  of an airgap. For publish it is worse — the destination prune then runs
  against an under-complete keep set and deletes correct files out of a live
  tree. `incomplete_mirrors()` is that gate for both.
- **Ctrl-C.** Nothing in the codebase handled `KeyboardInterrupt`. Interrupting
  a sync mid-metadata-fetch could be swallowed by a provider's broad `except`,
  so the run "completed" and exited **0** — an operator's Ctrl-C was
  indistinguishable from a successful sync. `@interruptible` turns it into one
  line and exit 130 for every verb.
"""
import functools
import sys
from pathlib import Path

from mmw.core import config

# Shell convention for "killed by SIGINT" (128 + 2). Distinguishable from both
# success and an ordinary failure, which is the whole point: a cron wrapper can
# tell "someone stopped it" from "it broke".
INTERRUPTED = 130


def eprint(*args, **kwargs) -> None:
    """Print to stderr. Failure output belongs there — a cron job mailing only
    stderr must get the thing that broke, and nothing else."""
    print(*args, file=sys.stderr, **kwargs)


class ConfigError(Exception):
    """mmw.toml could not be loaded. `str(e)` is the one line to print."""


def load_config(cfg_dir: Path = Path(".")) -> config.Config:
    """Load mmw.toml, raising `ConfigError` with a readable one-liner.

    Every failure mode collapses to this: a missing file, a TOML syntax error,
    and a pydantic `ValidationError` from an unknown key are all "your config
    is not usable and here is why", never a traceback.
    """
    try:
        return config.load(cfg_dir)
    except FileNotFoundError:
        raise ConfigError("No config found. Run `mmw init` first.") from None
    except Exception as e:
        raise ConfigError(f"Invalid config at {config.config_path(cfg_dir)}:\n{e}") from None


def load_config_or_none(cfg_dir: Path = Path("."), err=eprint) -> "config.Config | None":
    """`load_config` for the commands that return an exit code rather than raise."""
    try:
        return load_config(cfg_dir)
    except ConfigError as e:
        err(str(e))
        return None


def interruptible(fn):
    """Turn a Ctrl-C inside a command into one line and exit 130."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except KeyboardInterrupt:
            eprint("\nInterrupted.")
            return INTERRUPTED
    return wrapper


def incomplete_mirrors(net, cfg, mirror_dir: Path) -> list[str]:
    """One line per mirror of `net` the pool cannot fully serve. Read-only.

    The same check `tui/publish_flows.PublishFlows._run_pre_publish` runs, reached the same
    way — `resolve.network_mirrors()` for the mirror set, `verify_mirror()` for
    each. The pool is opened read-only, so this is safe alongside a running
    sync and creates nothing on disk.
    """
    from mmw.core import resolve
    from mmw.core.pool import Pool
    from mmw.core.verify import verify_mirror

    mirrors = resolve.network_mirrors(net, cfg, mirror_dir)
    problems: list[str] = []
    with Pool(mirror_dir, read_only=True) as pool:
        for m in mirrors:
            r = verify_mirror(m, mirror_dir, pool)
            if r.error:
                problems.append(f"{r.mirror_name}: {r.error}")
            elif not r.complete:
                problems.append(f"{r.mirror_name}: {r.describe()}")
    return problems


def preflight_ok(verb: str, net, cfg, mirror_dir: Path, allow_incomplete: bool,
                 warn_only: bool = False, *, stdout_is_payload: bool = False) -> bool:
    """False when `net` must not be published/bundled. Prints the reason.

    `--allow-incomplete` is an escape hatch, not a silence: it still says what
    is missing, it just doesn't refuse.

    `warn_only` is for `--dry-run`, which writes nothing and prunes nothing.
    The gate exists because a publish *deletes* against an under-complete keep
    set and a bundle *ships* a silently short tar; a dry run does neither, so
    refusing to even estimate would withhold the very report that tells the
    operator what to sync. It still prints every problem it found.
    """
    from mmw.core.db_warm import warm_for_job
    # `--out -`: stdout is the bundle itself, so even progress goes to stderr.
    say = eprint if stdout_is_payload else print
    warm_for_job(mirror_dir, say=lambda message: say(f"  {message}"))
    try:
        problems = incomplete_mirrors(net, cfg, mirror_dir)
    except Exception as e:
        # A pre-flight that cannot run is not a licence to skip it: an
        # unreadable pool is exactly when the thing it guards against is most
        # likely. `--allow-incomplete` remains the way through.
        if allow_incomplete:
            eprint(f"  ⚠ completeness check failed ({e}) — continuing anyway")
            return True
        eprint(f"Network '{net.name}': completeness check could not run: {e}")
        eprint("  Pass --allow-incomplete to go ahead regardless.")
        return False

    if not problems:
        return True

    for p in problems:
        eprint(f"  ✗ {p}")
    if warn_only:
        eprint(f"  ⚠ {len(problems)} incomplete mirror(s) — the estimate below "
               f"covers only what the pool can actually serve")
        return True
    if allow_incomplete:
        eprint(f"  ⚠ {len(problems)} incomplete mirror(s) — continuing (--allow-incomplete)")
        return True
    eprint(
        f"Refusing to {verb} network '{net.name}': {len(problems)} incomplete mirror(s). "
        "Sync first, or pass --allow-incomplete."
    )
    return False


def write_single_source(cfg_dir: Path, source, describe, *, dry_run: bool, echo) -> int:
    """Write a provider's one-per-config source: add it, or **replace** the
    existing one (saying what it replaced) — never merge. `--dry-run` prints
    the TOML and needs no config."""
    import tomli_w
    if dry_run:
        echo(tomli_w.dumps({"sources": [config.source_to_dict(source)]}))
        return 0
    try:
        cfg = load_config(cfg_dir)
    except ConfigError as e:
        eprint(str(e))
        return 1
    existing = next((s for s in cfg.sources if s.provider == source.provider), None)
    sources = [source if s is existing else s for s in cfg.sources]
    if existing is None:
        sources.append(source)
    config.save(cfg.model_copy(update={"sources": sources}), cfg_dir)
    if existing is None:
        echo(f"Added the {source.provider} source.")
    else:
        echo(f"Replaced the {source.provider} source (was: {describe(existing)}; "
             f"now: {describe(source)}).")
    return 0
