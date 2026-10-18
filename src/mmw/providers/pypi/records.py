"""What a project page needs about each file a unit refs.

Written by every unit as `.pypi-meta/files.json.gz`, so publish builds pages
from the pool alone: a snapshot or bundle replays exactly, without the live
index cache. Records of retained files are carried from the previous sync.
"""
import gzip
import json
from dataclasses import asdict, dataclass

from mmw.core.config import normalize_url, retention_days

RECORDS_PATH = ".pypi-meta/files.json.gz"


@dataclass(frozen=True)
class Record:
    project: str
    version: str
    filename: str
    url: str
    sha256: str
    size: int
    requires_python: str | None
    yanked: bool | str
    metadata_sha256: str | None
    upload_time: str | None


def record_of(f, upload_time: str | None = None) -> Record:
    return Record(project=f.project, version=f.version, filename=f.filename, url=f.url, sha256=f.sha256,
                  size=f.size, requires_python=f.requires_python, yanked=f.yanked,
                  metadata_sha256=f.metadata_sha256, upload_time=upload_time or f.upload_time)


def load(raw: bytes | None) -> dict[str, Record]:
    if not raw:
        return {}
    try:
        rows = json.loads(gzip.decompress(raw))
        return {path: Record(**fields) for path, fields in rows.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def dump(records: dict[str, Record]) -> bytes:
    rows = {path: asdict(records[path]) for path in sorted(records)}
    return gzip.compress(json.dumps(rows, separators=(",", ":"), sort_keys=True).encode(), mtime=0)


def widest_window(cfg, source_identity: str) -> int | None:
    """The longest retention of any network selecting the source — None for
    forever. A network selects it when its select is empty or names it; dims
    are not consulted, which only ever keeps more. No network: 0 (current)."""
    widest = 0
    for network in cfg.networks:
        if network.select and not any(normalize_url(s.source) == source_identity for s in network.select):
            continue
        days = retention_days(network.retention)
        if days is None:
            return None
        widest = max(widest, days)
    return widest
