"""Quiet, repeatable Bash setup for installed commands and offline installers."""
import fcntl
import os
from pathlib import Path
import sys

from mmw.completion_bash import _SCRIPT
from mmw.core.disk_io import ops

_MARKER = b'# >>> mmw bash completion >>>'
_BOOTSTRAP = b'''
# >>> mmw bash completion >>>
if [ -n "${BASH_VERSION:-}" ]; then
    _mmw_completion_file="${XDG_DATA_HOME:-$HOME/.local/share}/bash-completion/completions/mmw"
    [ ! -r "$_mmw_completion_file" ] || . "$_mmw_completion_file"
    unset _mmw_completion_file
fi
# <<< mmw bash completion <<<
'''


def install(prefix: Path | None = None) -> None:
    """Best effort: no output, network, shell detection or external programs.

    Append under a lock, preserving existing bytes, permissions and symlinks.
    The small bootstrap stays fixed while the generated script can be updated.
    An installer prefix also receives the standard package completion file.
    """
    script = (_SCRIPT % dict(complete_func='_mmw_completion',
                            autocomplete_var='_MMW_COMPLETE', prog_name='mmw')).encode()
    if prefix is not None:
        try:
            ops.write_if_changed(prefix / 'share/bash-completion/completions/mmw', script)
        except (OSError, ValueError):
            pass
    try:
        home = Path.home()
        data = Path(os.environ.get('XDG_DATA_HOME') or home / '.local/share')
        # The same lock serializes concurrent first launches, including the
        # atomic script replacement's staging path.
        with ops.open_file(home / '.bashrc', 'a+b') as rc:
            fcntl.flock(rc, fcntl.LOCK_EX)
            ops.write_if_changed(data / 'bash-completion/completions/mmw', script)
            rc.seek(0)
            existing = rc.read()
            if _MARKER not in existing:
                rc.write(_BOOTSTRAP)
    except (OSError, RuntimeError, ValueError):
        # Unwritable homes and read-only installations must still run mmw.
        # Retrying next launch also repairs an interrupted installation.
        pass


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def on_launch() -> None:
    # Tab is read-only; cron, SSH commands and pipelines don't alter profiles.
    if (os.environ.get('_MMW_COMPLETE') or '_MMW_BASH_LINE' in os.environ
            or not interactive()):
        return
    install()
