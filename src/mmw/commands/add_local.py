"""`mmw add local` — a folder of .deb files as a signed repository."""
from pathlib import Path

from pydantic import ValidationError

from mmw.commands.common import ConfigError, eprint, load_config
from mmw.core import config


def run(cfg_dir: Path, *, folder, url, structured, suite, component, key, contents,
        dry_run, echo) -> int:
    if not folder or not url:
        eprint("mmw add local needs --folder and --url.")
        return 2
    if not structured and (suite or component):
        eprint("--suite and --component lay out dists/, so they need --structured.")
        return 2
    layout = {k: v for k, v in (("suite", suite), ("component", component)) if v}
    try:
        source = config.LocalAptSource(provider="apt-local", url=url, folder=folder,
                                       key=key or "pending", structured=structured,
                                       contents=contents, **layout)
    except ValidationError as e:
        eprint(f"Not added: {e.errors()[0]['msg']}")
        return 2
    if key is None:
        source.key = source.name             # a key named after the repository
    if dry_run:
        import tomli_w
        echo(tomli_w.dumps({"sources": [config.source_to_dict(source)]}))
        return 0
    try:
        cfg = load_config(cfg_dir)
    except ConfigError as e:
        eprint(str(e))
        return 1
    from mmw.core.signing import Keyring, SigningError
    keyring = Keyring(cfg.settings.mirror_dir)
    try:
        have = {k.name for k in keyring.list()}
        if source.key not in have:
            if key is not None:
                eprint(f"no signing key named {key!r} — make one with `mmw signing-key generate {key}`")
                return 1
            keyring.generate(source.key)
            echo(f"generated signing key {source.key!r}")
    except SigningError as e:
        eprint(str(e))
        return 1
    existing = next((s for s in cfg.sources if s.identity == source.identity), None)
    sources = [source if s is existing else s for s in cfg.sources]
    if existing is None:
        sources.append(source)
    config.save(cfg.model_copy(update={"sources": sources}), cfg_dir)
    echo(f"{'replaced' if existing else 'added'} {source.name}: {config.source_to_dict(source)['url']} "
         f"← {source.folder}")
    return 0
