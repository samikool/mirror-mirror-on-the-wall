"""Read the state recorded by kept bundles without reading their payload."""
import json
import tarfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from zstandard import ZstdError

from mmw.core import disk_io
from mmw.core.publisher.manifest import RECORD_PATH, _open_text, is_manifest


@dataclass(frozen=True)
class KeptState:
    created: str
    where: str
    checksums: frozenset[str]
    settings: str | None = None      # the selection it was made with (format 3)


@contextmanager
def _text(path: Path):
    if is_manifest(path.name):
        with _open_text(path) as text:
            yield text
    else:
        with disk_io.ops.open_file(path, "rb") as raw, tarfile.open(fileobj=raw, mode="r|") as tar:
            member = tar.next()
            if member is None or member.name != RECORD_PATH or not member.isfile():
                raise ValueError(f"first member must be {RECORD_PATH}")
            with tar.extractfile(member) as stream:
                yield stream


def _header(text):
    header = json.loads(text.readline())
    if not isinstance(header, dict) or not isinstance(header.get("network"), str):
        raise ValueError("missing network")
    from datetime import datetime
    datetime.strptime(header["created"], "%Y%m%dT%H%M%SZ")
    if header.get("format") not in (2, 3):
        raise ValueError("expected manifest format 2 or 3")
    return header


def read_manifest_header(path: Path) -> dict | None:
    try:
        with _text(path) as text:
            return _header(text)
    except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, ZstdError):
        return None


def kept_states(dirs: list[Path], network: str, *, skipped: list[str]) -> list[KeptState]:
    states = []
    seen = set()
    for directory in dirs:
        if not disk_io.ops.is_dir(directory):
            continue
        for path in sorted(disk_io.ops.iterdir(directory)):
            if path.name.endswith(".tmp") and (path.name[:-4].endswith(".tar") or is_manifest(path.name[:-4])):
                if path not in seen:
                    skipped.append(f"{path}: incomplete temporary file")
                    seen.add(path)
                continue
            if path in seen or not (path.name.endswith(".tar") or is_manifest(path.name)):
                continue
            seen.add(path)
            try:
                with _text(path) as text:
                    header = _header(text)
                    if header["network"] != network:
                        continue
                    sums = set()
                    for line in text:
                        if line.strip():
                            row = json.loads(line)
                            if not isinstance(row["c"], str) or not row["c"]:
                                raise ValueError("missing checksum")
                            sums.add(row["c"])
                    settings = header.get("settings")
                    states.append(KeptState(header["created"], str(path), frozenset(sums),
                                            settings if isinstance(settings, str) and settings else None))
            except (OSError, ValueError, KeyError, TypeError, tarfile.TarError, ZstdError) as exc:
                skipped.append(f"{path}: {exc}")
    return sorted(states, key=lambda state: (state.created, state.where.endswith(".tar"), state.where))


def state_at(dirs, network, when: str, *, skipped) -> KeptState | None:
    eligible = [state for state in kept_states(dirs, network, skipped=skipped) if state.created <= when]
    return eligible[-1] if eligible else None
