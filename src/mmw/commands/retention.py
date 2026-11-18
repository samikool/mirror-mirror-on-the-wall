"""Shared configuration command for repository history retention."""
from mmw.core import config


def configure(cfg, *, keep, repo=None, suites=None):
    """Return validated retention changes; repo=None sets the global default."""
    if isinstance(keep, str) and keep.isdecimal():
        keep = int(keep)
    if repo is None:
        if suites is not None:
            raise ValueError('Per-suite retention needs a repository')
        data = cfg.settings.model_dump()
        data['keep_points'] = config.check_keep(keep, 'keep_points')
        return cfg.model_copy(update={'settings': config.Settings.model_validate(data)})
    from mmw.core.repos import resolve_repo_refs
    names = {s.identity: s.name for s in cfg.sources if isinstance(s, config.SourceConfig)}
    key = resolve_repo_refs([repo], names)[0]
    sources = []
    for source in cfg.sources:
        if source.identity == key:
            data = source.model_dump()
            data['keep_points'] = None if keep is None else config.check_keep(keep, 'keep_points')
            if suites is not None:
                data['keep_suites'] = suites
            source = config.SourceConfig.model_validate(data)
        sources.append(source)
    return cfg.model_copy(update={'sources': sources})


def run(cfg_dir, value, repo=None, suite=None):
    from mmw.commands.common import load_config_or_none, eprint
    cfg = load_config_or_none(cfg_dir)
    if cfg is None:
        return 1
    try:
        keep = None if value == 'default' and repo else config.check_keep(int(value) if value.isdecimal() else value, 'keep_points')
        suites = None
        if suite:
            from mmw.core.repos import resolve_repo_refs
            if not repo:
                raise ValueError('--suite needs --repo')
            key = resolve_repo_refs([repo], {s.identity: s.name for s in cfg.sources})[0]
            source = next(s for s in cfg.sources if s.identity == key)
            suites = dict(getattr(source, 'keep_suites', {}))
            if keep is None:
                suites.pop(suite, None)
            else:
                suites[suite] = keep
            keep = getattr(source, 'keep_points', None)
        changed = configure(cfg, keep=keep, repo=repo, suites=suites)
        config.save(changed, cfg_dir)
    except (ValueError, LookupError, OSError) as e:
        eprint(str(e))
        return 1
    print(f'History retention saved for {repo or "the repository default"}.')
    return 0
