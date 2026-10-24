"""A site's home: the directory holding its state (database, settings,
lock), on the share beside the root it serves, so any machine that mounts
the share sees the same site."""
import os
from pathlib import Path

import tomli_w
from pydantic import BaseModel

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from mmw.core import disk_io

SETTINGS = "site.toml"
STORE = Path(".mmw") / "store"


class SiteSettings(BaseModel):
    root: Path                          # the served tree
    keep_versions: int | None = None    # per repository; None keeps every version


def resolve_home(explicit: Path | None) -> Path:
    if explicit is not None:
        return Path(explicit)
    env = os.environ.get("MMW_SITE_HOME")
    if env:
        return Path(env)
    raise ValueError("pass --home or set MMW_SITE_HOME")


def default_home(root: Path) -> Path:
    return Path(root) / ".mmw" / "site"


def init_site(home: Path, root: Path) -> SiteSettings:
    home, root = Path(home), Path(root)
    if disk_io.ops.exists(home / SETTINGS):
        raise FileExistsError(f"{home} is already a site")
    disk_io.ops.mkdir(home, parents=True, exist_ok=True)
    disk_io.ops.mkdir(root / STORE, parents=True, exist_ok=True)
    settings = SiteSettings(root=root.resolve())
    save_settings(home, settings)
    return settings


def save_settings(home: Path, settings: SiteSettings) -> None:
    data = {"root": str(settings.root)}
    if settings.keep_versions is not None:
        data["keep_versions"] = settings.keep_versions
    disk_io.ops.atomic_write(Path(home) / SETTINGS, tomli_w.dumps(data).encode())


def load_site(home: Path) -> SiteSettings:
    path = Path(home) / SETTINGS
    if not disk_io.ops.exists(path):
        raise FileNotFoundError(f"no site at {home} — `mmw site init` first")
    return SiteSettings.model_validate(tomllib.loads(disk_io.ops.read_text(path)))
