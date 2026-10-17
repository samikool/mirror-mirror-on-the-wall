"""`mmw move-database PATH` — the Settings page's Move…, headless."""
from pathlib import Path

import typer

from mmw.commands.common import eprint, load_config_or_none
from mmw.core.database_move import DatabaseMoveError, move_database


def run(cfg_dir: Path, dest: Path) -> int:
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1
    shown = [-1]

    def progress(done: int, total: int) -> None:
        tenth = done * 10 // total if total else 10
        if tenth != shown[0]:
            shown[0] = tenth
            typer.echo(f"  copied {done:,} of {total:,} bytes")
    warnings: list[str] = []
    try:
        new = move_database(cfg_dir, cfg, dest, progress=progress, warnings=warnings)
    except DatabaseMoveError as e:
        eprint(f"Database not moved: {e}")
        return 1
    typer.echo(f"Database moved to {new.settings.database}")
    for warning in warnings:
        eprint(f"  warning: {warning}")
    return 0
