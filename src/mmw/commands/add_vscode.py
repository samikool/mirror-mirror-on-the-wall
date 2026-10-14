"""`mmw add vscode` — write (or replace) the config's one VS Code source."""
from pathlib import Path

import tomli_w
from pydantic import ValidationError

from mmw.commands.common import ConfigError, eprint, load_config
from mmw.core import config


def _read_ids(path: Path) -> list[str]:
    ids = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            ids.append(line)
    return ids


def _describe(source) -> str:
    from mmw.providers import get_provider
    return dict(get_provider("vscode").describe(source).fields)["Extensions"]


def run(cfg_dir: Path, *, kinds, platforms, qualities, top_n, ids, ids_file, all_,
        prerelease, dry_run, echo) -> int:
    """Write the config's VS Code source. There is only ever one, so running
    this again **replaces** it (and says what it replaced) — it never merges."""
    try:
        wanted = (_read_ids(ids_file) if ids_file else []) + list(ids or [])
        source = config.VscodeSource(
            provider="vscode", kinds=list(kinds or []), platforms=list(platforms or []),
            qualities=list(qualities or []),
            extensions=config.ExtensionSelection(top_n=top_n, ids=wanted, all=all_,
                                                 prerelease=prerelease))
    except (ValidationError, OSError) as e:
        eprint(f"Not added: {e}")
        return 2
    if dry_run:
        # Like `mmw add --dry-run` for apt files: a preview needs no config.
        echo(tomli_w.dumps({"sources": [config.source_to_dict(source)]}))
        return 0
    try:
        cfg = load_config(cfg_dir)
    except ConfigError as e:
        eprint(str(e))
        return 1
    existing = next((s for s in cfg.sources if s.provider == "vscode"), None)
    sources = [source if s is existing else s for s in cfg.sources]
    if existing is None:
        sources.append(source)
    config.save(cfg.model_copy(update={"sources": sources}), cfg_dir)
    if existing is None:
        echo("Added the vscode source.")
    else:
        echo(f"Replaced the vscode source (was: {_describe(existing)}; "
             f"now: {_describe(source)}).")
    return 0
