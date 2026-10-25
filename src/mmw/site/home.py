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


class RootConfig(BaseModel):
    name: str
    path: Path                          # where mmw sees it (this machine's mount)
    serve_path: Path                    # where the serving host sees it (Compose mounts)


class SiteSettings(BaseModel):
    roots: list[RootConfig]             # the first is the primary
    keep_versions: int | None = None    # per repository; None keeps every version
    # The site's CA, by path only: mmw never copies the key, never keeps its passphrase.
    ca_cert: Path | None = None
    ca_key: Path | None = None

    @property
    def primary(self) -> RootConfig:
        return self.roots[0]

    @property
    def root(self) -> Path:
        """The primary root's path."""
        return self.primary.path

    def root_named(self, name: str | None) -> RootConfig:
        if name is None:
            return self.primary
        for r in self.roots:
            if r.name == name:
                return r
        raise ValueError(f"no root {name!r} — this site has: {', '.join(r.name for r in self.roots)}")


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
    root = root.resolve()
    settings = SiteSettings(roots=[RootConfig(name="main", path=root, serve_path=root)])
    save_settings(home, settings)
    return settings


def save_settings(home: Path, settings: SiteSettings) -> None:
    data: dict = {"roots": [{"name": r.name, "path": str(r.path), "serve_path": str(r.serve_path)}
                            for r in settings.roots]}
    if settings.keep_versions is not None:
        data["keep_versions"] = settings.keep_versions
    if settings.ca_cert is not None:
        data["ca_cert"], data["ca_key"] = str(settings.ca_cert), str(settings.ca_key)
    disk_io.ops.atomic_write(Path(home) / SETTINGS, tomli_w.dumps(data).encode())


def load_site(home: Path) -> SiteSettings:
    path = Path(home) / SETTINGS
    if not disk_io.ops.exists(path):
        raise FileNotFoundError(f"no site at {home} — `mmw site init` first")
    data = tomllib.loads(disk_io.ops.read_text(path))
    if "root" in data and "roots" not in data:        # a site made before roots had names
        root = data.pop("root")
        data["roots"] = [{"name": "main", "path": root, "serve_path": root}]
    return SiteSettings.model_validate(data)


def add_root(home: Path, name: str, path: Path, serve_path: Path | None = None) -> RootConfig:
    settings = load_site(home)
    if any(r.name == name for r in settings.roots):
        raise ValueError(f"this site already has a root called {name!r}")
    path = Path(path).resolve()
    root = RootConfig(name=name, path=path, serve_path=Path(serve_path) if serve_path else path)
    disk_io.ops.mkdir(path / STORE, parents=True, exist_ok=True)
    save_settings(home, settings.model_copy(update={"roots": [*settings.roots, root]}))
    return root
