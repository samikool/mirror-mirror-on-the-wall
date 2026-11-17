"""CLI and Setup share recovery preparation; saving remains config.save's job."""
from pathlib import Path

from mmw.commands.common import eprint
from mmw.core import config, disk_io
from mmw.core.config_from_bundle import config_from_bundles


def parse(text: str) -> config.Config:
    """Validate the recovered configuration or an edited review of it."""
    return config.Config.model_validate(config.tomllib.loads(text))


def prepare(paths: list[Path], mirror_dir: Path) -> tuple[config.Config, list[str]]:
    text, notes = config_from_bundles(paths, mirror_dir)
    return parse(text), notes


def run(cfg_dir: Path, paths: list[Path], mirror_dir: Path, *, dry_run: bool = False) -> int:
    path = config.config_path(cfg_dir)
    if not dry_run and (disk_io.ops.exists(path) or disk_io.ops.is_symlink(path)):
        eprint("mmw.toml exists — --dry-run prints what it would write")
        return 1
    try:
        cfg, notes = prepare(paths, mirror_dir)
        for note in notes:
            eprint(note)
        if dry_run:
            print(config.dumps(cfg), end="")
        else:
            try:
                path = config.save(cfg, cfg_dir, overwrite=False)
            except FileExistsError:
                eprint("mmw.toml exists — --dry-run prints what it would write")
                return 1
            print(f"Wrote {path}")
    except (ValueError, OSError) as exc:
        eprint(str(exc))
        return 1
    return 0
