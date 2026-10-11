"""`mmw snapshot` — take, list, rename, delete and clean network snapshots.

A snapshot is a network's complete published tree at a moment, kept in the
pool database (see core/snapshots.py) so it can be bundled to or published
again exactly. Deleting one only drops its rows; the next `mmw prune` frees
the files no live mirror and no other snapshot still references.
"""
from pathlib import Path

from mmw.commands.common import eprint, load_config_or_none
from mmw.core.format import fmt_size
from mmw.core.pool import Pool, SnapshotInfo


def _network(cfg_dir: Path, name: str):
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return None, None
    net = next((n for n in cfg.networks if n.name == name), None)
    if net is None:
        eprint(f"No network named {name!r} — one of: "
               f"{', '.join(n.name for n in cfg.networks) or '(none configured)'}")
        return cfg, None
    return cfg, net


def _row(s: SnapshotInfo, holds: int | None = None) -> str:
    label = s.label or ""
    held = f"  holds {fmt_size(holds)}" if holds is not None else ""
    note = "" if s.complete else "  (history only — some files pruned)"
    return f"  {s.stamp}  {label:<20}  {s.kind:<8}  {s.files:>9,} files{held}{note}"


def take(cfg_dir: Path, network: str, label: str | None = None) -> int:
    from mmw.core import snapshots
    cfg, net = _network(cfg_dir, network)
    if net is None:
        return 1
    try:
        with Pool(cfg.settings.mirror_dir) as pool:
            snapshots.import_ledger(pool, cfg.settings.mirror_dir, net.name)
        snap = snapshots.take_snapshot(net, cfg, cfg.settings.mirror_dir, label=label)
    except Exception as e:  # noqa: BLE001 — one clear line, not a traceback
        eprint(f"Snapshot not taken: {e}")
        return 1
    print(f"Snapshot of {net.name} taken:")
    print(_row(snap))
    return 0


def list_(cfg_dir: Path, network: str) -> int:
    from mmw.core import snapshots
    cfg, net = _network(cfg_dir, network)
    if net is None:
        return 1
    with Pool(cfg.settings.mirror_dir) as pool:
        snapshots.import_ledger(pool, cfg.settings.mirror_dir, net.name)
        snaps = pool.snapshots(net.name)
        if not snaps:
            print(f"No snapshots of {net.name} yet — `mmw snapshot take -n {net.name}`.")
            return 0
        print(f"Snapshots of {net.name} (newest first):")
        for s in snaps:
            print(_row(s, pool.snapshot_exclusive_bytes(s.id)))
    return 0


def rename(cfg_dir: Path, network: str, ref: str, label: str) -> int:
    return _with_snapshot(cfg_dir, network, ref,
                          lambda pool, s: (pool.rename_snapshot(s.id, label),
                                           print(f"{s.stamp} is now labelled {label!r}.")))


def delete(cfg_dir: Path, network: str, ref: str) -> int:
    return _with_snapshot(cfg_dir, network, ref,
                          lambda pool, s: (pool.delete_snapshot(s.id),
                                           print(f"Deleted {s.stamp}"
                                                 + (f" ({s.label})" if s.label else "")
                                                 + " — `mmw prune` frees its files.")))


def _with_snapshot(cfg_dir, network, ref, act) -> int:
    from mmw.core import snapshots
    cfg, net = _network(cfg_dir, network)
    if net is None:
        return 1
    with Pool(cfg.settings.mirror_dir) as pool:
        try:
            snap = snapshots.find_snapshot(pool, net.name, ref)
        except LookupError as e:
            eprint(str(e))
            return 1
        act(pool, snap)
    return 0


def clean(cfg_dir: Path, network: str, keep: int, yes: bool = False) -> int:
    from mmw.core import snapshots
    cfg, net = _network(cfg_dir, network)
    if net is None:
        return 1
    with Pool(cfg.settings.mirror_dir) as pool:
        plan = snapshots.clean_plan(pool, net.name, keep)
        if not plan.delete:
            print(f"{net.name} has {keep} snapshot(s) or fewer — nothing to clean.")
            return 0
        verb = "Deleted" if yes else "Would delete"
        print(f"{verb} {len(plan.delete)} snapshot(s) of {net.name}, keeping the newest {keep}:")
        for s in plan.delete:
            print(_row(s))
        print(f"`mmw prune` then frees {fmt_size(plan.frees)}.")
        if yes:
            snapshots.delete_snapshots(pool, plan.delete)
            print(f"deleted {len(plan.delete)}.")
        else:
            print(f"would delete {len(plan.delete)} — run again with --yes to delete them.")
    return 0
