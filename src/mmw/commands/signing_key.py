"""`mmw signing-key` — the keys mmw signs repositories with."""
import sys
from pathlib import Path

import typer

from mmw.commands.common import eprint, load_config_or_none
from mmw.core import disk_io


def _keyring(cfg_dir: Path):
    from mmw.core.signing import Keyring
    cfg = load_config_or_none(cfg_dir)
    return (cfg, Keyring(cfg.settings.mirror_dir)) if cfg is not None else (None, None)


def _run(cfg_dir: Path, action) -> int:
    from mmw.core.signing import SigningError
    cfg, keyring = _keyring(cfg_dir)
    if keyring is None:
        return 1
    try:
        return action(cfg, keyring) or 0
    except SigningError as e:
        eprint(str(e))
        return 1


def generate(cfg_dir: Path, name: str) -> int:
    def act(cfg, kr):
        key = kr.generate(name)
        typer.echo(f"generated {key.name}: {key.fingerprint}")
    return _run(cfg_dir, act)


def import_(cfg_dir: Path, name: str, file: Path | None) -> int:
    def act(cfg, kr):
        text = sys.stdin.read() if file is None or str(file) == "-" else disk_io.ops.read_text(file)
        key = kr.import_armored(text, name)
        typer.echo(f"imported {key.name}: {key.fingerprint}")
    return _run(cfg_dir, act)


def export(cfg_dir: Path, name: str, private: bool) -> int:
    def act(cfg, kr):
        if private:
            eprint("This is the PRIVATE key: anyone holding it can sign as you. Keep it safe.")
            typer.echo(kr.private_armor(name), nl=False)
        else:
            typer.echo(kr.public_armor(name), nl=False)
    return _run(cfg_dir, act)


def list_(cfg_dir: Path) -> int:
    def act(cfg, kr):
        keys = kr.list()
        if not keys:
            typer.echo("No signing keys. Make one with `mmw signing-key generate NAME`.")
        for k in keys:
            users = [s.name for s in cfg.sources if getattr(s, "key", None) == k.name]
            typer.echo(f"{k.name}  {k.fingerprint}  {k.created}"
                       + (f"  signs {', '.join(users)}" if users else ""))
    return _run(cfg_dir, act)


def remove(cfg_dir: Path, name: str) -> int:
    def act(cfg, kr):
        users = [s.name for s in cfg.sources if getattr(s, "key", None) == name]
        if users:
            eprint(f"{name!r} signs {', '.join(users)} — change that source first")
            return 1
        kr.remove(name)
        typer.echo(f"removed {name}")
    return _run(cfg_dir, act)
