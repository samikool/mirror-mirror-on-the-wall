"""Per-network layers: custom debs a network adds to an upstream repository.

The pool keeps upstream pure. A layer is built (with the pool writable) into
replacement rows — the layer's debs, the rewritten `Packages`/`Contents`
indexes, the rewritten `Release` and its new or dropped signatures — stored
per (network, base) in `layer_rows`. Publish and snapshots walk the pool
read-only and swap those rows into the group (`publish_group(overlay=…)`),
so ordering, by-hash, prune, snapshots and bundles carry what is served.
"""
from pathlib import Path

LAYERS_DIR = "layers"


def layer_dir(mirror_dir: Path, network: str, repo_name: str) -> Path:
    """Where a network's custom debs for one repository live:
    `{suite}/{component}/*.deb` below it."""
    return Path(mirror_dir) / LAYERS_DIR / network / repo_name
