"""A unit's previous refs, readable — for providers that must carry earlier
state forward (VS Code keeps the previous two server commits; the upstream
API only names the latest)."""

from mmw.core import disk_io



class UnitHistory:
    """Reads the unit's refs on first use — most units never ask."""

    def __init__(self, pool, mirror_name: str):
        self._pool = pool
        self._name = mirror_name
        self._loaded: dict[str, str] | None = None

    @classmethod
    def empty(cls) -> "UnitHistory":
        return cls(None, "")

    @property
    def _refs(self) -> dict[str, str]:
        if self._loaded is None:
            self._loaded = self._pool.ref_map(self._name) if self._pool is not None else {}
        return self._loaded

    def checksum(self, repo_path: str) -> str | None:
        return self._refs.get(repo_path)

    def paths(self, prefix: str) -> list[str]:
        return sorted(p for p in self._refs if p.startswith(prefix))

    def read(self, repo_path: str) -> bytes | None:
        if self._pool is None or repo_path not in self._refs:
            return None
        path = self._pool.resolve(self._name, repo_path)
        try:
            return disk_io.ops.read_bytes(path) if path is not None else None
        except OSError:
            return None
