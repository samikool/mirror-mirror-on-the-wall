"""Per-route disk accounting for one job."""
import threading
from dataclasses import dataclass, field


@dataclass
class RouteStats:
    ops: int = 0
    seconds: float = 0.0
    bytes: int = 0


@dataclass
class DiskStats:
    """Per route: ops, seconds of wall time inside the route, bytes (when
    known). Summed into the existing timing outputs (`VerifyResult.timings`,
    `MirrorSummary.timings`, the publish result) as `disk <route>`."""

    routes: dict[str, RouteStats] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(self, route: str, ops: int, seconds: float, nbytes: int = 0) -> None:
        with self._lock:
            r = self.routes.setdefault(route, RouteStats())
            r.ops += ops
            r.seconds += seconds
            r.bytes += nbytes

    def timings(self) -> dict[str, float]:
        with self._lock:
            return {f"disk {route}": r.seconds for route, r in self.routes.items() if r.ops}
