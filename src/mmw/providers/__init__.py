from mmw.providers.apt import AptFlatProvider, AptProvider
from mmw.providers.base import BaseProvider
from mmw.providers.pypi.provider import PypiProvider
from mmw.providers.vscode.provider import VscodeProvider

# Registry order is meaningful: it is the order providers are offered in, the
# order their `contribute()` runs, and the order select keys are written.
_REGISTRY: dict[str, BaseProvider] = {
    "apt": AptProvider(),
    "apt-flat": AptFlatProvider(),
    "vscode": VscodeProvider(),
    "pypi": PypiProvider(),
}


def get_provider(provider_type: str) -> BaseProvider:
    p = _REGISTRY.get(provider_type)
    if p is None:
        raise ValueError(f"Unknown provider '{provider_type}'. Available: {', '.join(_REGISTRY)}")
    return p


def all_providers() -> list[BaseProvider]:
    return list(_REGISTRY.values())


def extra_hosts(network, units) -> list[str]:
    """Every provider's extra hostnames for `network`, first-seen order."""
    seen: list[str] = []
    for provider in all_providers():
        for host in provider.extra_hosts(network, units):
            if host not in seen:
                seen.append(host)
    return seen


def preview_all(network, cfg, pool, mirror_dir, result) -> None:
    """Every provider's dry-run additions, in registry order."""
    for provider in all_providers():
        provider.preview(network, cfg, pool, mirror_dir, result)
