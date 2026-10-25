"""Which root each repository lives in, and that root's store."""
import json
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

    @property
    def multi(self) -> bool:
        """nginx generated per root: several roots here, or (v2) a set of several roots."""
        return len(self.all) > 1 or len(self.settings.members) > 1

    def serves_extras(self, root: RootConfig) -> bool:
        """Whether `root` serves the network's own hostnames (upstream and
        extra hosts): the primary here, or in a set its first member."""
        if self.settings.set_id is not None and self.settings.members:
            return self.settings.members[0] == self.settings.name
        return root.name == self.primary.name

    def network_files(self, db, root: RootConfig) -> dict[str, str]:
        """The network-level files this root serves from its store. One root
        serves them all, as shipped. With several, nginx's text files are
        generated per root instead (`generated`), the primary serves the rest,
        and every other root also gets the keys and the nginx image."""
        files = db.network_files()
        if not self.multi:
            return files
        if root.name == self.primary.name:
            return {p: c for p, c in files.items() if not _nginx_text(p)}
        return {p: c for p, c in files.items()
                if p.startswith(EVERY_ROOT) or (p.startswith("nginx/") and p.endswith(".tar"))}

    def generated(self, db, root: RootConfig) -> dict[str, str]:
        """nginx files generated for this root: path → sha256."""
        return json.loads(db.get_meta(f"nginx:{root.name}", "{}"))

    def referenced(self, db, root: RootConfig) -> set[str]:
        """Every blob this root's store must keep."""
        kept = set(self.network_files(db, root).values())
        if root.name == self.primary.name:
            kept |= set(db.network_files().values())     # nginx's shipped text too: deltas assume it
        return db.referenced_checksums(self.placed_in(db, root), network=False) | kept


def _nginx_text(path: str) -> bool:
    return path.startswith("nginx/") and not path.endswith(".tar")
