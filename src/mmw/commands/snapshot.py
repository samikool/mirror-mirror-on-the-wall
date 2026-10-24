"""`mmw snapshot` — take, list, rename, delete and clean network snapshots.

A snapshot is a network's complete published tree at a moment, kept in the
pool database (see core/snapshots.py) so it can be bundled to or published
again exactly. Deleting one only drops its rows; the next `mmw prune` frees
the files no live mirror and no other snapshot still references.
"""
from pathlib import Path
from types import SimpleNamespace

from mmw.commands.common import eprint, load_config_or_none
from mmw.core.format import fmt_size
from mmw.core.pool import Pool, SnapshotInfo


def _network(cfg_dir: Path, name: str, gone_ok: bool = False):
    """(cfg, network). With `gone_ok`, a network no longer in the config but
    still holding snapshots in the pool is returned as a bare name: its
    snapshots can be listed, renamed and deleted, never taken or bundled.
    Without that, a network hand-edited out of mmw.toml left snapshots that
    pinned their files in the pool with no command able to reach them."""
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return None, None
    net = next((n for n in cfg.networks if n.name == name), None)
    if net is None and gone_ok:
        with Pool(cfg.settings.mirror_dir, read_only=True) as pool:
            if name in pool.network_names():
                print(f"({name} is no longer in the config — its snapshots can be "
                      "listed and deleted.)")
                return cfg, SimpleNamespace(name=name)
    if net is None:
        eprint(f"No network named {name!r} — one of: "
               f"{', '.join(n.name for n in cfg.networks) or '(none configured)'}")
        return cfg, None
    return cfg, net


def _row(s: SnapshotInfo, holds: int | None = None, serves: list[str] = ()) -> str:
    label = s.label or ""
    held = f"  holds {fmt_size(holds)}" if holds is not None else ""
    note = "" if s.complete else "  (history only — some files pruned)"
    note += "".join(f"  [published at {r}]" for r in serves)
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


def list_(cfg_dir: Path, network: str, repos: bool = False) -> int:
    from mmw.core import snapshots
    from mmw.core.repos import repository_label
    cfg, net = _network(cfg_dir, network, gone_ok=True)
    if net is None:
        return 1
    with Pool(cfg.settings.mirror_dir) as pool:
        snapshots.import_ledger(pool, cfg.settings.mirror_dir, net.name)
        snapshots.ensure_repo_versions(pool, net.name)
        snaps = pool.snapshots(net.name)
        if not snaps:
            print(f"No snapshots of {net.name} yet — `mmw snapshot take -n {net.name}`.")
            return 0
        print(f"Snapshots of {net.name} (newest first):")
        for s in snaps:
            print(_row(s, pool.snapshot_exclusive_bytes(s.id), snapshots.served_by(pool, s)))
            if repos:
                versions = sorted(pool.snapshot_repo_versions(s.id).values(),
                                  key=lambda v: repository_label(v.repo, cfg).lower())
                for v in versions:
                    changed = " (changed)" if v.stamp == s.stamp else ""
                    print(f"      {repository_label(v.repo, cfg)} v{v.number}{changed}")
    return 0


def rename(cfg_dir: Path, network: str, ref: str, label: str) -> int:
    def act(pool, s):
        pool.rename_snapshot(s.id, label)
        print(f"{s.stamp} is now labelled {label!r}.")
        return 0
    return _with_snapshot(cfg_dir, network, ref, act)


def delete(cfg_dir: Path, network: str, ref: str, yes: bool = False) -> int:
    from mmw.core import snapshots

    def act(pool, s):
        if not yes and s.stamp == snapshots.last_bundled_to(pool, s.network):
            eprint(f"{s.stamp} is where the last bundle of {s.network} went — without it "
                   "the next bundle needs --from (or --from none). Run again with --yes to delete it.")
            return 1
        try:
            snapshots.delete_snapshot(pool, s)
        except ValueError as e:
            eprint(str(e))
            return 1
        print(f"Deleted {s.stamp}" + (f" ({s.label})" if s.label else "")
              + " — `mmw prune` frees its files.")
        return 0
    return _with_snapshot(cfg_dir, network, ref, act)


def _with_snapshot(cfg_dir, network, ref, act) -> int:
    from mmw.core import snapshots
    cfg, net = _network(cfg_dir, network, gone_ok=True)
    if net is None:
        return 1
    with Pool(cfg.settings.mirror_dir) as pool:
        try:
            snap = snapshots.find_snapshot(pool, net.name, ref)
        except LookupError as e:
            eprint(str(e))
            return 1
        return act(pool, snap)


def _forget(pool: Pool, name: str, yes: bool) -> int:
    """`clean --keep 0` for a network no longer in the config: everything
    goes. Nothing can bundle from or publish its snapshots any more, so the
    "last bundle went here" and "published there" protections mean nothing."""
    snaps = pool.snapshots(name)
    frees = pool.snapshot_exclusive_bytes([s.id for s in snaps])
    print(f"{'Deleted' if yes else 'Would delete'} all {len(snaps)} snapshot(s) of {name} "
          "and its bundle history:")
    for s in snaps:
        print(_row(s))
    print(f"`mmw prune` then frees {fmt_size(frees)}.")
    if yes:
        pool.forget_network(name)
    else:
        print("run again with --yes to delete them.")
    return 0


def clean(cfg_dir: Path, network: str, keep: int, yes: bool = False) -> int:
    from mmw.core import snapshots
    cfg, net = _network(cfg_dir, network, gone_ok=True)
    if net is None:
        return 1
    with Pool(cfg.settings.mirror_dir) as pool:
        if isinstance(net, SimpleNamespace) and keep == 0:
            return _forget(pool, net.name, yes)
        plan = snapshots.clean_plan(pool, net.name, keep)
        for snap, why in plan.kept:
            print(f"Keeping {snap.stamp}" + (f" ({snap.label})" if snap.label else "")
                  + f" — {why}.")
        if not plan.delete:
            print(f"Nothing of {net.name} to clean.")
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
