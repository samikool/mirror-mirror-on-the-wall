"""`mmw merge OLDER NEWER` — and the TUI's Repositories → Merge into…"""
from __future__ import annotations

import sys
from pathlib import Path

from mmw.commands.common import eprint


def preview(cfg, older: str, newer: str):
    """(older key, newer key, plan) — read only."""
    from mmw.core.pool import Pool
    from mmw.core.repo_merge import plan_merge, resolve
    o, n = resolve(cfg, older), resolve(cfg, newer)
    with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
        return o, n, plan_merge(pool, cfg, o, n)


def merge_into(cfg, cfg_dir: Path | None, older: str, newer: str, *, confirmed: bool = False):
    """Fold `older` into `newer` and save the config (`cfg_dir` None: the
    caller saves `result.cfg` — the TUI, through its own config lock) — the
    one function the CLI and the TUI both call. A MergeResult."""
    from mmw.core.pool import Pool, shared_pool_lock
    from mmw.core.repo_merge import merge_repositories
    mirror_dir = Path(cfg.settings.mirror_dir)
    with shared_pool_lock(mirror_dir), Pool(mirror_dir) as pool:
        return merge_repositories(pool, cfg, older, newer, config_dir=cfg_dir, confirmed=confirmed)


def describe(plan) -> str:
    from mmw.core.repo_merge import PROOF_SAID
    moves = ", ".join(f"{a} → {b}" for a, b in sorted(plan.tracks.items()) if a != b) or "the same paths"
    return (f"{plan.older} into {plan.newer}: {plan.points} point(s), {moves} — "
            f"{PROOF_SAID.get(plan.proof, 'nothing shows they are one repository')}")


def run(cfg, cfg_dir: Path, older: str, newer: str, *, dry_run: bool = False, yes: bool = False,
        say=print) -> int:
    try:
        o, n, plan = preview(cfg, older, newer)
    except (LookupError, ValueError) as e:
        eprint(str(e))
        return 1
    if plan.already:
        say(f"{o} and {n} are already one repository")
        return 0
    say(describe(plan))
    if plan.proof is None:
        eprint(f"nothing shows they are one repository ({o}, {n}): no identical Release, no suite in common "
               "— merging them would mix two archives' histories")
        return 1
    if dry_run:
        return 0
    if not yes:
        if not sys.stdin.isatty():
            eprint("pass --yes to merge them (no terminal to ask on)")
            return 1
        if input(f"Merge {o} into {n}? [y/N] ").strip().lower() not in ("y", "yes"):
            return 1
    try:
        result = merge_into(cfg, cfg_dir, o, n, confirmed=True)
    except (LookupError, ValueError, PermissionError) as e:
        eprint(str(e))
        return 1
    for line in result.lines:
        say(line)
    return 0
