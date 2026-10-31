"""`mmw packages` — your own .debs: into a folder repository, or into an
upstream mirror as one network serves it."""
import sys
from pathlib import Path

import typer

from mmw.commands.common import eprint, load_config_or_none
from mmw.core import config


def _run(cfg_dir: Path, act) -> int:
    from mmw.core.layers import PackagesError
    from mmw.core.signing import SigningError
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1
    try:
        return act(cfg) or 0
    except PackagesError as e:
        eprint(str(e))
        return 2 if e.usage else 1
    except SigningError as e:
        eprint(str(e))
        return 1


def _ask(clash) -> str:
    what = ("the same version as upstream's, with different contents" if clash.kind == "same-version"
            else f"older than upstream's {clash.upstream_version}, so clients would not pick it")
    while True:
        answer = input(f"{clash.filename}: {clash.package} {clash.version} is {what}. "
                       "[k]eep mine / [d]rop mine / [c]ancel? ").strip().lower()[:1]
        if answer in ("k", "d", "c"):
            return {"k": "keep", "d": "drop", "c": "cancel"}[answer]


def add(cfg_dir: Path, paths, *, into, network, suite, component, resign, unsigned, on_clash) -> int:
    from mmw.core.layers import add_packages
    if resign and unsigned:
        eprint("--resign and --unsigned are one choice — pass one")
        return 2

    def act(cfg):
        signing = "resign" if resign else "unsigned" if unsigned else None
        ask = _ask if on_clash is None and sys.stdin.isatty() else None
        out = add_packages(cfg, cfg.settings.mirror_dir, paths, into=into, network=network,
                           suite=suite, component=component, signing=signing, key=resign,
                           on_clash=on_clash, ask=ask, config_dir=cfg_dir)
        if out.cfg is not cfg:
            config.save(out.cfg, cfg_dir)
        for p in out.copied:
            typer.echo(f"added {p}")
        for p in out.skipped:
            typer.echo(f"already there: {p.name}")
        for note in out.notes:
            typer.echo(note)
    return _run(cfg_dir, act)


def list_(cfg_dir: Path, network) -> int:
    from mmw.core.layers import list_packages

    def act(cfg):
        rows = list_packages(cfg, cfg.settings.mirror_dir, network, config_dir=cfg_dir)
        if not rows:
            typer.echo("No custom packages.")
        for where, location, name in rows:
            typer.echo(f"{where}  {location}  {name}")
    return _run(cfg_dir, act)


def remove(cfg_dir: Path, filename, *, into, network) -> int:
    from mmw.core.layers import remove_package

    def act(cfg):
        for p in remove_package(cfg, cfg.settings.mirror_dir, filename, into=into, network=network,
                                config_dir=cfg_dir):
            typer.echo(f"removed {p}")
    return _run(cfg_dir, act)


def signing(cfg_dir: Path, into, *, network, resign, unsigned, upstream, contents, drop_valid_until) -> int:
    from mmw.core.layers import set_signing
    if sum(bool(x) for x in (resign, unsigned, upstream)) > 1:
        eprint("--resign, --unsigned and --upstream are one choice — pass one")
        return 2

    def act(cfg):
        mode = "resign" if resign else "unsigned" if unsigned else "upstream" if upstream else None
        new = set_signing(cfg, network, into, signing=mode, key=resign, contents=contents,
                          drop_valid_until=drop_valid_until)
        config.save(new, cfg_dir)
        layer = next(n for n in new.networks if n.name == network).layer_for(
            next(s for s in new.sources if s.name == into).url)
        typer.echo(f"{into} on {network}: {layer.signing}"
                   + (f" with {layer.key}" if layer.key else "")
                   + (", apt-file support" if layer.contents else "")
                   + (", no Valid-Until" if layer.drop_valid_until else ""))
        if layer.signing == "unsigned":
            eprint("Unsigned is unsafe: clients need [trusted=yes] for this repository.")
    return _run(cfg_dir, act)
