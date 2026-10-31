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


def _run(cfg_dir: Path, action, keyring=None) -> int:
    """`action(cfg, keyring)`: the config's keyring, or (far side) the one
    given — then `cfg` is None."""
    from mmw.core.signing import SigningError
    cfg = None
    if keyring is None:
        cfg, keyring = _keyring(cfg_dir)
    if keyring is None:
        return 1
    try:
        return action(cfg, keyring) or 0
    except SigningError as e:
        eprint(str(e))
        return 1
    finally:
        keyring.stop_agent()


def generate(cfg_dir: Path, name: str, keyring=None) -> int:
    def act(cfg, kr):
        key = kr.generate(name)
        typer.echo(f"generated {key.name}: {key.fingerprint}")
    return _run(cfg_dir, act, keyring)


def import_(cfg_dir: Path, name: str, file: Path | None, keyring=None) -> int:
    def act(cfg, kr):
        text = sys.stdin.read() if file is None or str(file) == "-" else disk_io.ops.read_text(file)
        key = kr.import_armored(text, name)
        typer.echo(f"imported {key.name}: {key.fingerprint}")
    return _run(cfg_dir, act, keyring)


def export(cfg_dir: Path, name: str, private: bool, keyring=None) -> int:
    def act(cfg, kr):
        if private:
            eprint("This is the PRIVATE key: anyone holding it can sign as you. Keep it safe.")
            typer.echo(kr.private_armor(name), nl=False)
        else:
            typer.echo(kr.public_armor(name), nl=False)
    return _run(cfg_dir, act, keyring)


def list_(cfg_dir: Path, keyring=None) -> int:
    def act(cfg, kr):
        keys = kr.list()
        if not keys:
            typer.echo("No signing keys. Make one with `mmw signing-key generate NAME`.")
        for k in keys:
            users = [s.name for s in cfg.sources if getattr(s, "key", None) == k.name] if cfg else []
            typer.echo(f"{k.name}  {k.fingerprint}  {k.created}"
                       + (f"  signs {', '.join(users)}" if users else ""))
    return _run(cfg_dir, act, keyring)


def remove(cfg_dir: Path, name: str, keyring=None, in_use=None) -> int:
    def act(cfg, kr):
        users = ([s.name for s in cfg.sources if getattr(s, "key", None) == name] if cfg
                 else list((in_use or (lambda _n: []))(name)))
        if users:
            eprint(f"{name!r} signs {', '.join(users)} — change that source first")
            return 1
        kr.remove(name)
        typer.echo(f"removed {name}")
    return _run(cfg_dir, act, keyring)


# ── the far side: a site root's own keyring ──────────────────────────────────

def _the_local_root(mset, root: Path | None):
    """The local root `--root` names (each root keeps its own keyring and
    local list), else the first on this machine."""
    from mmw.site.mset import LocalRoot
    local = [r for r in mset.roots if isinstance(r, LocalRoot)]
    if root is not None:
        want = Path(root).resolve()
        named = next((r for r in local if Path(r.info.path).resolve() == want), None)
        if named is not None:
            return named
    return local[0] if local else None

def site_keyring(root: Path | None):
    """The keyring of a local site root (the opened one by default), and a
    function naming the repositories whose site layer signs with a key."""
    from mmw.commands.site import _set
    from mmw.core.signing import Keyring
    mset = _set(root)
    local = _the_local_root(mset, root)
    if local is None:
        raise FileNotFoundError("no root of this mirror is on this machine — run it where one is")
    from mmw.site.db import SiteDB

    def in_use(name: str) -> list[str]:
        with SiteDB(local.home) as db:
            return [s.label for s in db.repo_states()
                    if (lay := db.layer(s.repo)) is not None and lay.key == name]
    return Keyring(local.home), in_use
