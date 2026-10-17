"""Reading many small pool files — a VS Code mirror's 120k metadata JSONs —
with many reads in flight. One at a time, each costs a full seek on a spinning
disk; a queue of them the drive reorders, several times faster. Blobs are
content-addressed, so what one pass reads stays right for the next."""
from collections.abc import Callable, Hashable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

WORKERS = 16          # keeps the disk's queue full; 32 left the TUI's thread waiting its turn


def _read_bytes(path) -> bytes:
    return Path(path).read_bytes()


def read_all(paths: Iterable[Hashable], *, workers: int = WORKERS,
             reader: Callable[[Hashable], bytes] | None = None) -> dict:
    """{path: its bytes, or the OSError reading it raised} for every path."""
    reader = reader or _read_bytes
    def one(path):
        try:
            return path, reader(path)
        except OSError as exc:
            return path, exc
    paths = list(dict.fromkeys(paths))
    if not paths:
        return {}
    with ThreadPoolExecutor(max_workers=min(workers, len(paths))) as pool:
        return dict(pool.map(one, paths))
