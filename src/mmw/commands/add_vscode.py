"""`mmw add vscode` — write (or replace) the config's one VS Code source."""
from pathlib import Path

from pydantic import ValidationError

from mmw.core import disk_io
from mmw.commands.common import eprint, write_single_source
from mmw.core import config


def _read_ids(path: Path) -> list[str]:
    ids = []
    for line in disk_io.ops.read_text(path).splitlines():
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
    return write_single_source(cfg_dir, source, _describe, dry_run=dry_run, echo=echo)
