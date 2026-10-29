"""`mmw sources-file NAME` — the client sources file for a folder repository."""
from pathlib import Path

import typer

from mmw.commands.common import eprint, load_config_or_none


def run(cfg_dir: Path, name: str, fmt: str) -> int:
    from mmw.providers.apt_local import sources_file
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1
    source = next((s for s in cfg.sources if s.provider == "apt-local" and s.name == name), None)
    if source is None:
        eprint(f"no folder repository named {name}")
        return 1
    try:
        typer.echo(sources_file(source, fmt), nl=False)
    except ValueError as e:
        eprint(str(e))
        return 2
    return 0
