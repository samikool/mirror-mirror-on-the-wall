from mmw.providers.apt import AptFlatProvider, AptProvider
from mmw.providers.base import BaseProvider

_REGISTRY: dict[str, BaseProvider] = {
    "apt": AptProvider(),
    "apt-flat": AptFlatProvider(),
}


def get_provider(provider_type: str) -> BaseProvider:
    p = _REGISTRY.get(provider_type)
    if p is None:
        raise ValueError(f"Unknown provider '{provider_type}'. Available: {', '.join(_REGISTRY)}")
    return p
