"""What a network's far side runs, and the one rule that follows from it.

An `apply` network's far side has only `apply.py`, which applies
whole-network bundles in order. Partial bundles and `mmw stage` only make
sense to a far side running mmw, which tracks each repository's version.
"""
from mmw.core.config import NetworkConfig


def require_mmw(network: NetworkConfig, what: str) -> None:
    """Refuse `what` on a network whose far side runs only apply.py."""
    if network.far_side != "mmw":
        raise ValueError(f"network '{network.name}' is an apply.py network — "
                         f'set far_side = "mmw" to use {what}')
