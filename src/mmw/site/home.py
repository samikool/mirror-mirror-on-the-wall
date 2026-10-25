"""A site's home: the directory holding its state (database, settings,
lock), on the share beside the root it serves, so any machine that mounts
the share sees the same site."""
import os
import uuid
from dataclasses import dataclass
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
ROOT_HOME = Path(".mmw") / "site"            # every root is its own site: its home is here


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
    # v2: the root's identity — its set (one connected-side network's roots),
    # its own name, and the names (never paths) of every root in the set.
    set_id: str | None = None
    network: str | None = None
    name: str | None = None
    members: list[str] = []
    hosts: dict[str, str] = {}          # hostname → the name of the root holding it, set-wide

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
    for key in ("set_id", "network", "name"):
        if getattr(settings, key) is not None:
            data[key] = getattr(settings, key)
    if settings.members:
        data["members"] = list(settings.members)
    if settings.hosts:
        data["hosts"] = dict(sorted(settings.hosts.items()))
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
    settings = SiteSettings.model_validate(data)
    if settings.set_id is not None:
        # A v2 root is wherever its home is mounted now: paths don't travel.
        here = Path(home).resolve().parent.parent
        r = settings.roots[0]
        moved = r.serve_path == r.path
        settings.roots[0] = r.model_copy(update={"path": here, "serve_path": here if moved else r.serve_path})
    return settings


def add_root(home: Path, name: str, path: Path, serve_path: Path | None = None) -> RootConfig:
    settings = load_site(home)
    if any(r.name == name for r in settings.roots):
        raise ValueError(f"this site already has a root called {name!r}")
    path = Path(path).resolve()
    root = RootConfig(name=name, path=path, serve_path=Path(serve_path) if serve_path else path)
    disk_io.ops.mkdir(path / STORE, parents=True, exist_ok=True)
    save_settings(home, settings.model_copy(update={"roots": [*settings.roots, root]}))
    return root


# ── v2: every root its own site ──────────────────────────────────────────────

def root_home(root: Path) -> Path:
    return Path(root) / ROOT_HOME


def new_set_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class RootInfo:
    location: str                       # as the user gave it (recents keep this)
    path: Path
    home: Path
    name: str
    set_id: str | None
    network: str | None
    members: tuple[str, ...]


def init_root(root: Path, *, name: str, set_id: str, network: str, serve_path: Path | None = None) -> Path:
    """Make `root` a root of set `set_id`; its home. A root of another set is refused."""
    home = root_home(root)
    if disk_io.ops.exists(home / SETTINGS):
        have = load_site(home)
        if have.set_id != set_id:
            raise ValueError(f"{root} already holds the mirror of network {have.network!r} — "
                             f"not {network!r}; pick another root")
        return home
    settings = init_site(home, root)
    r = settings.roots[0]
    if serve_path is not None:
        settings.roots[0] = r.model_copy(update={"serve_path": Path(serve_path)})
    save_settings(home, settings.model_copy(update={"set_id": set_id, "network": network, "name": name,
                                                    "members": [name]}))
    return home


def read_root(location: str) -> RootInfo | None:
    """The root at `location`, or None: no mirror there (or nothing mounted)."""
    path = Path(location).expanduser()
    home = root_home(path)
    if not disk_io.ops.exists(home / SETTINGS):
        return None
    s = load_site(home)
    return RootInfo(location, path.resolve(), home, s.name or path.name, s.set_id, s.network,
                    tuple(s.members or [s.name or path.name]))


def hostnames(home: Path) -> set[str]:
    """Every hostname a root holds: the first component of its repositories' bases."""
    from mmw.site.db import SiteDB
    with SiteDB(home) as db:
        return {b.split("/", 1)[0] for st in db.repo_states() for b in st.bases}
