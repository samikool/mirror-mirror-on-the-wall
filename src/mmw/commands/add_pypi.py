"""`mmw add pypi` — write (or replace) the config's one PyPI source."""
from pathlib import Path

from pydantic import ValidationError

from mmw.commands.common import eprint, write_single_source
from mmw.core import config


def _describe(source) -> str:
    from mmw.providers import get_provider
    return get_provider("pypi").source_summary(source)


def run(cfg_dir: Path, *, pythons, platforms, arches, glibc, musl, macos, sdists, all_,
        dry_run, echo, requirements=(), requirements_file=None) -> int:
    """Write the config's PyPI source. There is only ever one, so running this
    again **replaces** it (and says what it replaced) — it never merges."""
    floors = {k: v for k, v in (("glibc", glibc), ("musl", musl), ("macos", macos)) if v}
    try:
        source = config.PypiSource(provider="pypi", pythons=list(pythons or []),
                                   platforms=list(platforms or []), arches=list(arches or []),
                                   sdists=sdists, all=all_, requirements=list(requirements or []),
                                   requirements_file=requirements_file, **floors)
    except ValidationError as e:
        eprint(f"Not added: {e}")
        return 2
    return write_single_source(cfg_dir, source, _describe, dry_run=dry_run, echo=echo)
