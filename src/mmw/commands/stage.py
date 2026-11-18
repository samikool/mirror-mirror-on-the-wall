"""Headless stage — `mmw stage`: the bundle laid out as a directory of
hardlinks (mmw networks only). Same checks and output as `mmw bundle`; only
the container differs."""
from pathlib import Path

from mmw.commands import bundle


def run(cfg_dir: Path, network: str, out: Path | None = None, allow_incomplete: bool = False,
        from_ref: str | None = None, to_ref: str | None = None,
        repos: list[str] | None = None, installer: bool = False, runtime_wheel: Path | None = None,
        history: bool | None = None, full: bool = False,
        since: str | None = None, kept: list[Path] | None = None, until: str | None = None, dry_run: bool = False) -> int:
    return bundle.run(cfg_dir, network=network, out=out, allow_incomplete=allow_incomplete,
                      from_ref=from_ref, to_ref=to_ref, repos=repos, stage=True,
                      installer=installer, runtime_wheel=runtime_wheel, history=history, full=full,
                      since=since, kept=kept, until=until, dry_run=dry_run)
