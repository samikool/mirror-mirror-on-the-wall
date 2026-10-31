"""Far-side layers: custom debs a site adds to a repository it serves.

A site layer never touches a stored version (deltas keep applying against
it, digest and all). Its rebuilt files are an *overlay* per (repository,
version, inputs digest); a repository serves `version ⊕ overlay`, so every
switch — apply, hold, roll back, forward — carries the layer. The rebuild is
the connected side's (`mmw.core.layers._rebuild`) over the site store.
"""
from pathlib import Path
from urllib.parse import quote

LAYERS_DIR = "layers"


def site_layer_dir(home: Path, repo: str) -> Path:
    """`{suite}/{component}/*.deb` below it; the repository key made one safe segment."""
    return Path(home) / LAYERS_DIR / quote(repo, safe="")
