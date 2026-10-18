"""When each of a unit's files was first kept, and when it left the keep set.

Stored as the unit's own `.pypi-meta/history.json.gz` blob, so it moves with
the unit's refs: a stopped sync reconciles nothing and the previous history
stays in force. Network retention (stage 3) decides from `left_at` how long
a file that left is still published; until then every left file is kept.
"""
import gzip
import json
from dataclasses import asdict, dataclass

from mmw.providers.base import PackageInfo

HISTORY_PATH = ".pypi-meta/history.json.gz"


@dataclass(frozen=True)
class Entry:
    repo_path: str
    url: str
    sha256: str
    size: int
    filename: str
    item_id: str
    first_kept: str            # ISO date
    left_at: str | None        # None while in the current keep set


def load(raw: bytes | None) -> dict[str, Entry]:
    if not raw:
        return {}
    try:
        rows = json.loads(gzip.decompress(raw))
        return {r["repo_path"]: Entry(**r) for r in rows}
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def advance(previous: dict[str, Entry], current: list[PackageInfo], today: str) -> dict[str, Entry]:
    out: dict[str, Entry] = {}
    for p in current:
        before = previous.get(p.repo_path)
        out[p.repo_path] = Entry(repo_path=p.repo_path, url=p.url, sha256=p.checksum, size=p.size,
                                 filename=p.filename, item_id=p.item_id,
                                 first_kept=before.first_kept if before else today, left_at=None)
    for path, e in previous.items():
        if path not in out:
            out[path] = e if e.left_at else Entry(**{**asdict(e), "left_at": today})
    return out


def as_packages(entries) -> list[PackageInfo]:
    return [PackageInfo(name=e.item_id, url=e.url, checksum=e.sha256, checksum_type="sha256", size=e.size,
                        repo_path=e.repo_path, filename=e.filename, item_id=e.item_id) for e in entries]


def retained(history: dict[str, Entry]) -> list[PackageInfo]:
    return as_packages(e for e in history.values() if e.left_at)


def dump(history: dict[str, Entry]) -> bytes:
    rows = [asdict(history[k]) for k in sorted(history)]
    return gzip.compress(json.dumps(rows, separators=(",", ":"), sort_keys=True).encode(), mtime=0)
