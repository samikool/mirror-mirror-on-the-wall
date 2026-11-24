"""Headless verify — `mmw verify`, the cron-safe door onto the same
completeness check the TUI runs before every publish (the Publish panel's
verify stage).

`pool.mirror_files()` — what both publish and bundle enumerate from — is
`refs ⋈ packages`, so a ref whose blob the pool doesn't hold is *invisible* to
it: an incomplete mirror publishes (or bundles) a tree silently missing files
rather than raising an error, and the gap only surfaces as a 404 during
`apt install` on the far side of an airgap. `verify_mirror` is what catches
this, but before this module it was reachable only by driving
the TUI's verify stage — undoable from cron. This check is
read-only: it never downloads anything.

`--stat` also stats every blob and `--hash` re-reads and hashes it — the
only checks that see a file truncated or deleted underneath the pool, and
O(pool) disk I/O, so never implicit. `--repair` drops what they find damaged
(`Pool.forget_blobs`), so the next `mmw sync` downloads it again; it is the
one write this command can make.
"""
from pathlib import Path

from mmw.commands.common import eprint, interruptible, load_config_or_none
from mmw.core import resolve
from mmw.core.format import fmt_size
from mmw.core.pool import Pool, PoolBusy

# Imported at module level, not inside _verify_all: tests monkeypatch this as
# a module attribute (`verify_cmd.verify_mirror = ...`), and an import inside
# the function body would bind a fresh reference to the real one on every
# call, silently defeating the patch. Same reasoning as `commands/bundle.py`'s
# module-level import of `bundle_network`.
from mmw.core.verify import VerifyResult, verify_mirror


def _verify_all(cfg, mirror_dir: Path, network: str | None, *,
                stat_blobs: bool = False, hash_blobs: bool = False,
                say=None, stop_event=None) -> list[VerifyResult]:
    """Verify every mirror the pool holds, or one network's slice of them.

    Split out from `run()` so tests can stub this one seam instead of
    constructing a real `Pool` — the pool it opens is read-only, so this is
    safe to run alongside an active sync and creates nothing on disk.
    """
    if network:
        # `run()` has already confirmed the name exists.
        net = next(n for n in cfg.networks if n.name == network)
        mirrors = resolve.network_mirrors(net, cfg, mirror_dir)
    else:
        mirrors = resolve.available_mirrors(cfg, mirror_dir)

    from mmw.core.db_warm import warm_for_job
    if say is None:
        warm_say = lambda message: print(f"  {message}")          # noqa: E731
        # A blob scan is minutes on a big pool: name its steps and progress.
        step_say = (lambda text: print(f"    {text}", flush=True)) if stat_blobs or hash_blobs else None
    else:
        warm_say = step_say = say
    warm_for_job(mirror_dir, say=warm_say, stop_event=stop_event)
    pool = Pool(mirror_dir, read_only=True)
    try:
        results = []
        for m in mirrors:
            if stop_event is not None and stop_event.is_set():
                break
            if say is not None:
                say(f"{m.name}…")
            results.append(verify_mirror(m, mirror_dir, pool, stat_blobs=stat_blobs,
                                         hash_blobs=hash_blobs, say=step_say, stop_event=stop_event))
        return results
    finally:
        pool.close()


def verify_pool(cfg, network: str | None = None, *, stat_blobs: bool = False, hash_blobs: bool = False,
                say=None, stop_event=None) -> list[VerifyResult]:
    """The pool check `mmw verify` and the TUI's Verify pool page both run:
    every mirror the pool holds (or one network's), read-only. `say(text)`
    names each step; a set `stop_event` ends it early."""
    if network and not any(n.name == network for n in cfg.networks):
        raise LookupError(f"Network '{network}' not found.")
    return _verify_all(cfg, cfg.settings.mirror_dir, network, stat_blobs=stat_blobs or hash_blobs,
                       hash_blobs=hash_blobs, say=say, stop_event=stop_event)


def damaged_blobs(results: list[VerifyResult]) -> set[str]:
    """The checksums a repair drops: blobs gone, the wrong size, or mis-hashed."""
    return {c for r in results for c in r.corrupt_checksums}


def repair_pool(cfg, results: list[VerifyResult]) -> tuple[int, int]:
    """Drop the damaged blobs `results` found, so the next sync downloads
    them again: (count, bytes). Raises PoolBusy while a sync holds the pool."""
    with Pool(cfg.settings.mirror_dir) as pool:
        return pool.forget_blobs(damaged_blobs(results))


@interruptible
def run(cfg_dir: Path = Path("."), network: str | None = None, stat_blobs: bool = False,
        hash_blobs: bool = False, repair: bool = False, points: bool = False) -> int:
    """Verify pool completeness for one or all mirrors. Returns a process exit code.

    `stat_blobs` stats every blob, `hash_blobs` (implies `stat_blobs`) hashes it;
    `repair` (implies `stat_blobs`) drops the damaged ones so the next sync fetches them."""
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1

    if network and not any(n.name == network for n in cfg.networks):
        eprint(f"Network '{network}' not found.")
        return 1

    if points:
        try:
            problems = check_points(cfg, network)
        except (OSError, ValueError) as e:
            eprint(str(e))
            return 1
        print(describe_points(problems))
        return int(bool(problems))

    stat_blobs = stat_blobs or hash_blobs or repair
    try:
        results = verify_pool(cfg, network, stat_blobs=stat_blobs, hash_blobs=hash_blobs)
    except OSError as e:
        eprint(f"Cannot read the pool in {cfg.settings.mirror_dir}: {e.strerror or e}")
        return 1

    if not results:
        print("No mirrors to verify.")
        return 0

    ok = True
    for r in results:
        if r.error:
            eprint(f"  {r.mirror_name:<40} ERROR: {r.error}")
            ok = False
        elif r.complete:
            print(f"  {r.mirror_name:<40} complete · {r.total:,} files")
        else:
            eprint(f"  {r.mirror_name:<40} {r.describe()}")
            for line in r.corrupt[:5]:
                eprint(f"      ✗ {line}")
            if len(r.corrupt) > 5:
                eprint(f"      … and {len(r.corrupt) - 5:,} more")
            ok = False

        if r.timings:
            print(f"    {r.timing_summary()}")

    complete_count = sum(1 for r in results if r.complete)
    print(f"\n{complete_count}/{len(results)} mirror(s) complete.")

    damaged = damaged_blobs(results)
    if damaged and repair:
        try:
            count, size = repair_pool(cfg, results)
        except PoolBusy as e:
            eprint(str(e))
            return 1
        print(f"Dropped {count:,} damaged blob(s) ({fmt_size(size)}) — "
              "run `mmw sync` to download them again.")
    elif damaged:
        eprint(f"{len(damaged):,} damaged blob(s) — `mmw verify --repair` drops them so "
               "the next `mmw sync` downloads them again.")

    # A completeness problem is an exit-1 condition — same rule
    # `mmw sync --quiet` documents: a cron job that silently reported nothing
    # wrong on an incomplete pool is worse than one that fails loudly. A
    # repaired pool is still short until the next sync.
    return 0 if ok else 1


def verify_points(pool, repos=None):
    """Check every held APT point, including retained pre-upgrade points."""
    from mmw.core.point_check import PointProblem, check_suite
    from mmw.core.points import _reader
    from mmw.site.versionref import day
    with pool._read() as conn:
        ids = [r[0] for r in conn.execute('SELECT id FROM points WHERE held = 1')]
    problems = []
    for point in pool.points_of(ids).values():
        track = pool.track_of_point(point.id)
        if repos is not None and track.repo not in repos:
            continue
        if not track.name or track.name == 'network':
            continue
        files = pool.point_files(point.id)
        found = check_suite(files, track.name, _reader(pool, files))
        if found:
            problems.append(PointProblem(track.repo, track.suite or track.name.rsplit('/', 1)[-1],
                                         day(point.date), found))
    return problems


def describe_points(problems):
    if not problems:
        return 'every kept point is consistent'
    return '\n'.join(f'{p.repo}: {p.suite} {p.date}: {problem}'
                     for p in problems for problem in p.problems)


def check_points(cfg, network=None):
    """Shared command used by the CLI and the Sources TUI."""
    from mmw.core.points import prepare_history
    from mmw.core.repos import repository_key
    prepare_history(cfg.settings.mirror_dir)
    repos = None
    if network:
        net = next(n for n in cfg.networks if n.name == network)
        repos = {repository_key(u) for u in resolve.network_mirrors(net, cfg, cfg.settings.mirror_dir)}
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        return verify_points(pool, repos)
