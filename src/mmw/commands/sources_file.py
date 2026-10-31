"""`mmw sources-file NAME` — the client sources file for a folder repository,
or (with --network) for an upstream mirror a network adds packages to."""
from pathlib import Path

import typer

from mmw.commands.common import eprint, load_config_or_none


def run(cfg_dir: Path, name: str, fmt: str, network: str | None = None) -> int:
    from mmw.providers.apt_local import sources_file
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1
    if network is not None:
        from mmw.core.layers import PackagesError, layer_sources_file
        try:
            typer.echo(layer_sources_file(cfg, cfg.settings.mirror_dir, network, name, fmt), nl=False)
        except PackagesError as e:
            eprint(str(e))
            return 2 if e.usage else 1
        return 0
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
