"""Which root each repository lives in, and that root's store."""
from pathlib import Path

from mmw.core import disk_io
from mmw.site.home import STORE, RootConfig, SiteSettings
from mmw.site.store import Store

# Network-level files every root needs: clients fetch keys from any host.
EVERY_ROOT = ("keys/",)


class Roots:
    def __init__(self, settings: SiteSettings):
        self.settings = settings
        self.all = list(settings.roots)
        self.primary = settings.primary
        self._stores: dict[str, Store] = {}

    def named(self, name: str | None) -> RootConfig:
        return self.settings.root_named(name)

    def of(self, state) -> RootConfig:
        """The root a repository is placed in (its RepoState; None: primary)."""
        return self.named(state.root if state is not None else None)

    def store(self, root: RootConfig) -> Store:
        if root.name not in self._stores:
            self._stores[root.name] = Store(Path(root.path))
        return self._stores[root.name]

    def check_mounted(self, root: RootConfig) -> None:
        if not disk_io.ops.is_dir(Path(root.path) / STORE):
            raise FileNotFoundError(f"root {root.name!r} at {root.path} is not there (not mounted?) — "
                                    "mount it, or nothing in it can change")

    def placed_in(self, db, root: RootConfig) -> set[str]:
        return {s.repo for s in db.repo_states() if self.of(s).name == root.name}

    def referenced(self, db, root: RootConfig) -> set[str]:
        """Every blob this root's store must keep."""
        primary = root.name == self.primary.name
        out = db.referenced_checksums(self.placed_in(db, root), network=primary)
        if not primary:
            for prefix in EVERY_ROOT:
                out |= db.referenced_checksums(set(), network=True, network_prefix=prefix)
        return out
