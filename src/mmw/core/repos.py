"""Repositories: what end users revert as one, and so what snapshots
version and the far side tracks.

An apt source is a repository, keyed by its normalized URL (the unit's
`source`). A provider whose `repository_scope` is "provider" is one
repository for all its sources, keyed by the provider name: VS Code moves as
a whole, so does PyPI. Every published path belongs to the repository of
the longest base that contains it, or to none: a network-level file (keys,
generated setup).
"""
from mmw.core.config import Config, MirrorConfig
from mmw.providers import get_provider


def repository_key(unit: MirrorConfig) -> str:
    provider = get_provider(unit.provider)
    return provider.name if provider.repository_scope == "provider" else unit.source


def repository_label(key: str, cfg: Config) -> str:
    """A repository's name for people: its source's name, the provider's
    label, or the key itself for one no longer configured."""
    try:
        provider = get_provider(key)
    except ValueError:
        provider = None
    if provider is not None and provider.repository_scope == "provider":
        return provider.label
    return next((s.name for s in cfg.sources if s.identity == key), key)


def attribute(path: str, repo_bases: dict[str, list[str]]) -> str | None:
    """The repository of the longest base `path` lies under, or None."""
    best, best_len = None, -1
    for repo, bases in repo_bases.items():
        for base in bases:
            if len(base) > best_len and path.startswith(base + "/"):
                best, best_len = repo, len(base)
    return best
