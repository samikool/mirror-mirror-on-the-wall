"""The Networks section's logic — no widgets, so it is testable on its own and
driven by the Networks section.

A network is a noun — a named downselect of the mirror; `root` and
`bundle_dir` are remembered destinations for the publish and bundle verbs.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from mmw.core import config
from mmw.core.pool import Pool
from mmw.core.publisher.manifest import ledger_dir
from mmw.core.resolve import available_mirrors, network_mirrors


def available(cfg: config.Config) -> list[config.MirrorConfig]:
    """Mirrors that actually exist — from the pool, not config expansion
    (architectures are discovered, so `cfg.mirrors` under-reports a repository
    mirroring "all" of them). **Blocking**: a pool query."""
    return available_mirrors(cfg, cfg.settings.mirror_dir)


def form_problems(*, name: str, existing_names: set[str], selects: list) -> str | None:
    """The first thing wrong with a network form, or None. Add and Edit share
    these checks — Add used to skip the name rule, which the model then
    enforced with a ValidationError instead of a message."""
    if not selects:
        return "Select at least one repository — nothing selected publishes nothing."
    if problem := name_problem(name, existing_names):
        return problem
    return None


def name_problem(name: str, existing_names: set[str]) -> str | None:
    if not config.NETWORK_NAME_RE.fullmatch(name or "") or name in (".", ".."):
        return ("Network name: letters, digits, '.', '_' and '-' only — it is used as a "
                "directory name.")
    if name in existing_names:
        return f"A network called '{name}' already exists."
    return None


def with_network(cfg: config.Config, net: config.NetworkConfig) -> config.Config:
    return cfg.model_copy(update={"networks": [*cfg.networks, net]})


def without_network(cfg: config.Config, name: str) -> config.Config:
    return cfg.model_copy(update={"networks": [n for n in cfg.networks if n.name != name]})


def replace_network(cfg: config.Config, old_name: str, net: config.NetworkConfig,
                    ) -> config.Config:
    """Swap the network called `old_name` for `net` (every field — the edit
    page returns a whole NetworkConfig, so nothing can be silently dropped)."""
    return cfg.model_copy(update={"networks": [
        net if n.name == old_name else n for n in cfg.networks]})


def rename_state(mirror_dir: Path, old: str, new: str) -> list[str]:
    """Move certs/ and bundles/ state to the new name. **Blocking.**"""
    if old == new:
        return []
    return config.rename_network_state(mirror_dir, old, new)


def mirror_names(net: config.NetworkConfig, cfg: config.Config) -> set[str]:
    """The mirrors a network publishes. **Blocking** (pool query)."""
    return {m.name for m in network_mirrors(net, cfg, cfg.settings.mirror_dir)}


def clear_ledger(mirror_dir: Path, name: str) -> str | None:
    """Delete a removed network's bundle ledger, and its snapshots, bundle
    log and publish records from the pool. **Blocking.**

    Not served content and not shared, so it goes with the network —
    otherwise a network added later under the same name would inherit stale
    delta history, and the snapshots would pin their files in the pool with
    no command able to reach them. A ledger is one manifest per bundle ever
    produced, so on a long-lived network this is not the two files it looks
    like."""
    problems = []
    if (mirror_dir / ".pool" / "mmw.db").exists():
        try:
            with Pool(mirror_dir) as pool:
                pool.forget_network(name)
        except Exception as e:  # noqa: BLE001 — reported, never fatal to the removal
            problems.append(f"Could not remove the snapshots of '{name}': {e}")
    ledger = ledger_dir(mirror_dir, name)
    if ledger.exists():
        try:
            shutil.rmtree(ledger)
        except OSError as e:
            problems.append(f"Could not remove bundle history for '{name}': {e}")
    return "; ".join(problems) or None


def exclusive_publish_dirs(
    net: config.NetworkConfig,
    mirror_map: dict[str, config.MirrorConfig],
    removed_names: set[str],
    kept_names: set[str],
) -> tuple[list[Path], list[str]]:
    """Split removed mirrors into (deletable dirs, bases still in use).

    One publish base serves every mirror of a source URL — all Ubuntu suites
    live under archive.ubuntu.com/ubuntu — so deleting the base of a removed
    mirror would take the kept suites with it. Only bases no kept mirror
    publishes to are safe to delete; the rest lose their stale files on the
    next publish's prune pass.

    A bundle-only network has no `root` — nothing was ever hardlinked to a
    tree, so there is nothing here to offer deleting.
    """
    if net.root is None:
        return [], []
    kept_bases = {mirror_map[n].mirror_base for n in kept_names if n in mirror_map}
    removed_bases = {mirror_map[n].mirror_base for n in removed_names if n in mirror_map}
    dirs = [
        net.root / b
        for b in sorted(removed_bases - kept_bases)
        if (net.root / b).exists()
    ]
    return dirs, sorted(removed_bases & kept_bases)


def stale_trees(old: config.NetworkConfig, new: config.NetworkConfig,
                mirror_map: dict[str, config.MirrorConfig], removed: set[str], kept: set[str],
                ) -> tuple[list[Path], list[str], str | None]:
    """What an edit left behind on disk: `(deletable dirs, shared bases, note)`.
    **Blocking** (stats the candidate dirs).

    Removed mirrors were published under the network's *old* root. When the
    edit also moved the root, every tree under the old root is now unserved —
    and the new root's directories are not this network's to offer (they may
    be another network's content) — so nothing is offered; the note says
    where the old tree is instead."""
    if old.root != new.root:
        if old.root is None:
            return [], [], None
        return [], [], (f"{old.root} is no longer published by this network — remove it "
                        "yourself if nothing else serves from it.")
    return (*exclusive_publish_dirs(old, mirror_map, removed, kept), None)
